from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, url_for
from flask_login import current_user, login_required

from app.exceptions import (
    ConsentRequired,
    HostConflictError,
    HostTransferGapError,
    IllegalStateTransition,
    PermissionDenied,
    RawTextImmutableError,
)
from app.graphs.ai_turn import AITurnGraph
from app.graphs.post_meeting import PostMeetingGraph
from app.models import ActionItem, Meeting, MeetingImportJob, ScheduledTask, SpeakerProfile, Terminology, TranscriptSegment, User
from app.models.enums import MeetingStatus
from app.security.rbac import can, require
from app.services.audio import AudioService
from app.services.audio_import import AudioImportService, ImportValidationError
from app.services.host import HostService
from app.services.knowledge import KnowledgeService
from app.services.meeting import MeetingService
from app.services.meeting_state import MeetingStateService
from app.services.speaker import SpeakerIDService
from app.services.task import TaskService
from app.services.transcript import TranscriptService
from app.web.extensions import csrf, db

api_bp = Blueprint("api", __name__, url_prefix="/api")
csrf.exempt(api_bp)


def _json_error(message: str, code: int):
    return jsonify({"error": message}), code


@api_bp.route("/health")
def api_health():
    from app.services.health import HealthService

    return jsonify(HealthService(current_app.extensions.get("providers")).check())


@api_bp.route("/internal/live-broadcast", methods=["POST"])
def internal_live_broadcast():
    """Localhost-only: speech worker asks the web process to emit to a Socket.IO room."""
    from app.services.speech_pipeline import is_loopback_addr
    from app.web.extensions import socketio
    from app.web.socket_events import NS

    if not is_loopback_addr(request.remote_addr):
        return _json_error("forbidden", 403)
    data = request.get_json(silent=True) or {}
    event = data.get("event")
    meeting_id = data.get("meeting_id")
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    if not event or not meeting_id:
        return _json_error("missing event or meeting_id", 400)
    socketio.emit(event, payload, namespace=NS, to=meeting_id)
    return jsonify({"ok": True})


@api_bp.route("/meetings", methods=["POST"])
@login_required
def create_meeting():
    data = request.get_json(silent=True) or request.form or {}
    try:
        m = MeetingService().create(
            current_user,
            name=data.get("name"),
            project_id=data.get("project_id"),
            expected_speakers=int(data.get("expected_speakers") or 4),
            meeting_type=data.get("meeting_type") or "其他",
        )
    except PermissionDenied as exc:
        return _json_error(str(exc), 403)
    return jsonify({"id": m.id, "name": m.name, "status": m.status}), 201


@api_bp.route("/meetings/import", methods=["POST"])
@login_required
def import_meeting_audio():
    """Accept one existing audio recording and enqueue local processing."""
    consent = str(request.form.get("consent") or "").lower() in {"1", "true", "on", "yes"}
    if not consent:
        return _json_error("请确认已取得与会人员录音和 AI 转写告知同意", 400)
    try:
        expected = int(request.form.get("expected_speakers") or 4)
    except (TypeError, ValueError):
        return _json_error("预计人数格式不正确", 400)
    try:
        meeting, job = AudioImportService(current_app).create_from_upload(
            current_user,
            request.files.get("audio"),
            name=request.form.get("name"),
            project_id=request.form.get("project_id"),
            expected_speakers=expected,
            meeting_type=request.form.get("meeting_type") or "其他",
        )
    except PermissionDenied as exc:
        return _json_error(str(exc), 403)
    except ImportValidationError as exc:
        return _json_error(str(exc), 400)
    except Exception:
        current_app.logger.exception("audio import upload failed")
        return _json_error("音频导入失败，请检查文件后重试", 500)
    payload = AudioImportService.status_payload(job)
    payload.update(
        {
            "id": meeting.id,
            "meeting_id": meeting.id,
            "status": meeting.status,
            "detail_url": url_for("meetings.detail", meeting_id=meeting.id),
        }
    )
    return jsonify(payload), 202


@api_bp.route("/meetings/<meeting_id>/import-status")
@login_required
def import_status(meeting_id: str):
    require(current_user, "view")
    job = MeetingImportJob.query.filter_by(meeting_id=meeting_id).first()
    if not job:
        return _json_error("import job not found", 404)
    payload = AudioImportService.status_payload(job)
    meeting = db.session.get(Meeting, meeting_id)
    payload["status"] = meeting.status if meeting else "FAILED"
    return jsonify(payload)


@api_bp.route("/meetings/<meeting_id>/processing-status")
@login_required
def processing_status(meeting_id: str):
    """Status endpoint shared by imported and browser-recorded meetings."""
    return import_status(meeting_id)


@api_bp.route("/meetings/<meeting_id>/import/retry", methods=["POST"])
@login_required
def retry_import(meeting_id: str):
    try:
        require(current_user, "create_meeting")
        job = AudioImportService(current_app).retry(meeting_id)
    except PermissionDenied as exc:
        return _json_error(str(exc), 403)
    except ImportValidationError as exc:
        return _json_error(str(exc), 409)
    return jsonify(AudioImportService.status_payload(job)), 202


@api_bp.route("/meetings/<meeting_id>/start", methods=["POST"])
@login_required
def start_meeting(meeting_id: str):
    meeting = MeetingService().get(meeting_id)
    if not meeting:
        return _json_error("not found", 404)
    data = request.get_json(silent=True) or {}
    if data.get("consent") or data.get("consent_given"):
        MeetingService().set_consent(meeting, True)
    try:
        MeetingService().start(meeting, current_user, client_id=data.get("client_id"))
        if data.get("client_id"):
            HostService().claim_host(meeting, data["client_id"], user_id=current_user.id)
    except PermissionDenied as exc:
        return _json_error(str(exc), 403)
    except ConsentRequired as exc:
        return _json_error(str(exc), 400)
    except (HostConflictError, IllegalStateTransition) as exc:
        return _json_error(str(exc), 409)
    return jsonify({"id": meeting.id, "status": meeting.status, "host_client_id": meeting.host_client_id})


@api_bp.route("/meetings/<meeting_id>/stop", methods=["POST"])
@login_required
def stop_meeting(meeting_id: str):
    meeting = MeetingService().get(meeting_id)
    if not meeting:
        return _json_error("not found", 404)
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    remaining = AudioService(current_app.extensions["audio_store"]).remaining(meeting_id)
    MeetingService().stop(meeting, remaining_chunks=remaining, force=force)
    if remaining == 0 or force:
        if force:
            # Retained for admin repair and deterministic tests. Normal browser
            # completion must return immediately and be handled by the worker.
            graph = PostMeetingGraph(current_app, current_app.extensions["providers"], current_app.extensions["audio_store"])
            graph.run(meeting_id)
            meeting = MeetingService().get(meeting_id)
            return jsonify({"id": meeting.id, "status": meeting.status, "remaining": remaining})
        try:
            job = AudioImportService(current_app).enqueue_live_postprocess(meeting_id)
        except ImportValidationError as exc:
            return _json_error(str(exc), 409)
        payload = AudioImportService.status_payload(job)
        payload.update({"id": meeting_id, "status": MeetingStatus.PROCESSING.value, "remaining": remaining})
        return jsonify(payload), 202
    return jsonify({"id": meeting.id, "status": meeting.status, "remaining": remaining}), 202


@api_bp.route("/meetings/<meeting_id>/host-transfer", methods=["POST"])
@login_required
def host_transfer(meeting_id: str):
    meeting = MeetingService().get(meeting_id)
    if not meeting:
        return _json_error("not found", 404)
    data = request.get_json(silent=True) or {}
    to_client = data.get("client_id") or data.get("to_client_id")
    if not to_client:
        return _json_error("client_id required", 400)
    from app.web.extensions import socketio

    old_host = meeting.host_client_id
    try:
        row, gap = HostService(current_app.config.get("HOST_TRANSFER_MAX_GAP_MS", 2000)).transfer(
            meeting,
            to_client,
            user_id=current_user.id,
        )
    except (HostConflictError, HostTransferGapError) as exc:
        return _json_error(str(exc), 409)
    payload = {
        "meeting_id": meeting_id,
        "host_client_id": row.client_id,
        "to_client_id": row.client_id,
        "previous_host_client_id": old_host,
        "from_client_id": old_host,
        "gap_ms": gap,
    }
    socketio.emit("host_changed", payload, namespace="/live", to=meeting_id)
    socketio.emit("host_transferred", payload, namespace="/live", to=meeting_id)
    return jsonify({"host_client_id": row.client_id, "gap_ms": gap})


@api_bp.route("/meetings/<meeting_id>/audio/chunks", methods=["POST"])
@login_required
def upload_chunk(meeting_id: str):
    seq = int(request.form.get("seq") or (request.json or {}).get("seq") or 0)
    start_ms = int(request.form.get("start_ms") or 0)
    mime = request.form.get("mime") or "audio/wav"
    client_id = request.form.get("client_id") or "web"
    f = request.files.get("file")
    if f:
        data = f.read()
        mime = f.mimetype or mime
    else:
        data = request.get_data() or b""
        if request.is_json:
            # tests may send json with base64 omitted; empty is invalid
            data = (request.json.get("data") or "").encode() if isinstance(request.json.get("data"), str) else data
    if not data:
        return _json_error("empty chunk", 400)
    result = AudioService(current_app.extensions["audio_store"]).ingest_chunk(
        meeting_id, client_id, seq, data, start_ms=start_ms, mime=mime
    )
    return jsonify({"ack": True, "seq": result["seq"], "idempotent": result.get("idempotent", False)})


@api_bp.route("/meetings/<meeting_id>/audio/chunks/status")
@login_required
def chunk_status(meeting_id: str):
    client_id = request.args.get("client_id")
    return jsonify(AudioService(current_app.extensions["audio_store"]).status(meeting_id, client_id))


@api_bp.route("/meetings/<meeting_id>/audio/complete", methods=["POST"])
@login_required
def audio_complete(meeting_id: str):
    data = request.get_json(silent=True) or {}
    result = AudioService(current_app.extensions["audio_store"]).complete(
        meeting_id, data.get("client_id") or "web", force=bool(data.get("force"))
    )
    return jsonify(result)


@api_bp.route("/meetings/<meeting_id>/transcript")
@login_required
def get_transcript(meeting_id: str):
    segs = TranscriptService().list_for_meeting(meeting_id)
    return jsonify(
        {
            "segments": [
                {
                    "id": s.id,
                    "raw_text": s.raw_text,
                    "punctuated_text": s.punctuated_text,
                    "sensevoice_text": s.sensevoice_text,
                    "corrected_text": s.corrected_text,
                    "final_text": s.final_text,
                    "display_text": s.display_text or s.corrected_text or s.raw_text,
                    "asr_state": s.asr_state,
                    "asr_revision": s.asr_revision,
                    "speaker_label": s.speaker_label,
                    "speaker_user_id": s.speaker_user_id,
                    "confidence": s.speaker_confidence,
                    "start_ms": s.start_ms,
                    "end_ms": s.end_ms,
                    "source": s.source,
                }
                for s in segs
            ]
        }
    )


@api_bp.route("/meetings/<meeting_id>/state")
@login_required
def get_state(meeting_id: str):
    return jsonify(MeetingStateService().current(meeting_id).to_dict())


@api_bp.route("/meetings/<meeting_id>/state", methods=["POST", "PATCH"])
@login_required
def patch_state(meeting_id: str):
    data = request.get_json(silent=True) or {}
    state = MeetingStateService().apply_patch(meeting_id, data.get("patch") or data)
    return jsonify(state.to_dict())


@api_bp.route("/meetings/<meeting_id>/segments/<seg_id>", methods=["PATCH"])
@login_required
def patch_segment(meeting_id: str, seg_id: str):
    seg = db.session.get(TranscriptSegment, seg_id)
    if not seg or seg.meeting_id != meeting_id:
        return _json_error("not found", 404)
    data = request.get_json(silent=True) or {}
    try:
        TranscriptService().patch(
            seg,
            corrected_text=data.get("corrected_text"),
            speaker_label=data.get("speaker") or data.get("speaker_label"),
            speaker_user_id=data.get("speaker_user_id"),
            raw_text=data.get("raw_text"),
            display_text=data.get("display_text"),
        )
    except RawTextImmutableError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"id": seg.id, "raw_text": seg.raw_text, "punctuated_text": seg.punctuated_text, "sensevoice_text": seg.sensevoice_text, "corrected_text": seg.corrected_text, "final_text": seg.final_text, "display_text": seg.display_text})


@api_bp.route("/terminology", methods=["GET", "POST"])
@login_required
def terminology():
    if request.method == "GET":
        rows = Terminology.query.order_by(Terminology.priority.desc(), Terminology.term.asc()).all()
        return jsonify([{"id": row.id, "term": row.term, "aliases": row.aliases, "description": row.description, "priority": row.priority, "enabled": row.enabled} for row in rows])
    if not can(current_user, "manage_terminology"):
        return _json_error("forbidden", 403)
    data = request.get_json(silent=True) or {}
    term = str(data.get("term") or "").strip()
    if not term:
        return _json_error("term required", 400)
    row = Terminology.query.filter_by(term=term).first() or Terminology(term=term)
    row.aliases = [str(value).strip() for value in data.get("aliases", []) if str(value).strip()]
    row.description = str(data.get("description") or "").strip()
    row.priority = int(data.get("priority") or 0)
    row.enabled = bool(data.get("enabled", True))
    db.session.add(row)
    db.session.commit()
    return jsonify({"id": row.id, "term": row.term}), 201


@api_bp.route("/meetings/<meeting_id>/reprocess", methods=["POST"])
@login_required
def reprocess(meeting_id: str):
    meeting = MeetingService().get(meeting_id)
    if not meeting:
        return _json_error("not found", 404)
    try:
        existing = MeetingImportJob.query.filter_by(meeting_id=meeting_id).first()
        job = (
            AudioImportService(current_app).retry(meeting_id)
            if existing
            else AudioImportService(current_app).enqueue_live_postprocess(meeting_id)
        )
    except ImportValidationError as exc:
        return _json_error(str(exc), 409)
    return jsonify({"id": meeting_id, "status": "PROCESSING", "job": AudioImportService.status_payload(job)}), 202


@api_bp.route("/action-items", methods=["GET", "POST"])
@login_required
def action_items():
    if request.method == "GET":
        rows = ActionItem.query.order_by(ActionItem.created_at.desc()).all()
        return jsonify([{"id": a.id, "description": a.description, "status": a.status, "owner": a.owner} for a in rows])
    data = request.get_json(silent=True) or {}
    item = ActionItem(
        description=data.get("description") or "",
        owner=data.get("owner") or "",
        meeting_id=data.get("meeting_id"),
        status=data.get("status") or "未开始",
        source_segment_id=data.get("source_segment_id"),
    )
    db.session.add(item)
    db.session.commit()
    return jsonify({"id": item.id}), 201


@api_bp.route("/action-items/<item_id>", methods=["PATCH", "DELETE"])
@login_required
def action_item_mut(item_id: str):
    item = db.session.get(ActionItem, item_id)
    if not item:
        return _json_error("not found", 404)
    if request.method == "DELETE":
        db.session.delete(item)
        db.session.commit()
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    for k in ("description", "owner", "status", "deadline", "priority"):
        if k in data:
            setattr(item, k, data[k])
    db.session.commit()
    return jsonify({"id": item.id, "status": item.status})


@api_bp.route("/scheduled-tasks", methods=["GET", "POST"])
@login_required
def scheduled_tasks():
    svc = TaskService()
    if request.method == "GET":
        return jsonify([{"id": t.id, "name": t.name, "cron_expression": t.cron_expression, "enabled": t.enabled} for t in svc.list_tasks()])
    data = request.get_json(silent=True) or {}
    t = svc.create(
        name=data.get("name") or "task",
        prompt=data.get("prompt") or "",
        cron_expression=data.get("cron_expression") or "0 9 * * *",
        created_by=current_user.id,
    )
    return jsonify({"id": t.id}), 201


@api_bp.route("/knowledge", methods=["GET", "POST"])
@login_required
def knowledge():
    svc = KnowledgeService()
    if request.method == "GET":
        q = request.args.get("q")
        if q:
            return jsonify({"hits": svc.search(q)})
        return jsonify([{"id": d.id, "title": d.title} for d in svc.list_documents()])
    data = request.get_json(silent=True) or {}
    doc = svc.create_document(data.get("title") or "doc", data.get("content") or "")
    return jsonify({"id": doc.id}), 201


@api_bp.route("/members", methods=["GET", "POST"])
@login_required
def members():
    if request.method == "GET":
        users = User.query.all()
        return jsonify([{"id": u.id, "email": u.email, "name": u.name, "role": u.role} for u in users])
    if not can(current_user, "manage_members"):
        return _json_error("forbidden", 403)
    data = request.get_json(silent=True) or {}
    u = User(email=data.get("email"), name=data.get("name") or "成员", role=data.get("role") or "Member")
    u.set_password(data.get("password") or "changeme")
    db.session.add(u)
    db.session.commit()
    return jsonify({"id": u.id}), 201


@api_bp.route("/meetings/<meeting_id>/master.wav")
@login_required
def master_wav(meeting_id: str):
    from flask import send_file

    meeting = MeetingService().get(meeting_id)
    if not meeting:
        return _json_error("not found", 404)
    require(current_user, "view")
    path = current_app.extensions["audio_store"].master_path(meeting_id)
    if not path.exists():
        return _json_error("master audio not found", 404)
    return send_file(path, mimetype="audio/wav", download_name="meeting_master.wav", as_attachment=False)


@api_bp.route("/speaker-profiles/<profile_id>/enroll", methods=["POST"])
@login_required
def enroll(profile_id: str):
    profile = db.session.get(SpeakerProfile, profile_id)
    if not profile:
        return _json_error("not found", 404)
    from app.services.enroll import EnrollError, enroll_profile

    audio = request.files.get("audio") or request.files.get("file")
    blob = audio.read() if audio else b""
    form_seconds = request.form.get("sample_seconds")
    json_body = request.get_json(silent=True) or {}
    if not blob and json_body.get("audio_b64"):
        import base64
        blob = base64.b64decode(json_body["audio_b64"])
    seconds = form_seconds or json_body.get("sample_seconds")
    try:
        enroll_profile(
            profile,
            blob,
            sample_seconds=float(seconds) if seconds not in (None, "") else None,
            embedding_provider=current_app.extensions["providers"].embedding,
            mime=(audio.mimetype if audio else json_body.get("mime")),
        )
    except EnrollError as exc:
        return _json_error(exc.message, exc.code)
    return jsonify({"id": profile.id, "sample_seconds": profile.sample_seconds, "enrolled_at": profile.enrolled_at.isoformat() if profile.enrolled_at else None})


@api_bp.route("/ai/query", methods=["POST"])
@login_required
def ai_query():
    data = request.get_json(silent=True) or {}
    prompt = data.get("query") or data.get("prompt") or ""
    meeting_id = data.get("meeting_id")
    providers = current_app.extensions["providers"]
    if meeting_id:
        graph = AITurnGraph(providers.llm, providers.tts)
        # A typed question is an explicit request, not a wake-word event.  It
        # must still use the canonical meeting prompt; the old bare completion
        # silently discarded speaker attribution and all meeting context.
        answer = providers.llm.complete(graph.build_prompt(meeting_id, prompt))
        return jsonify({"answer": answer, "silent": False})
    hits = KnowledgeService(providers.embedding).search(prompt)
    answer = providers.llm.complete(prompt + "\n" + str(hits))
    return jsonify({"answer": answer, "hits": hits})
