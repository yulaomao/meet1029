from __future__ import annotations

import json

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app.exceptions import ConsentRequired, PermissionDenied
from app.models import ActionItem, Decision, Meeting, MeetingImportJob, MeetingSummary, TranscriptSegment
from app.security.rbac import require
from app.services.audit import AuditService
from app.services.host import HostService
from app.services.meeting import MeetingService
from app.services.meeting_state import MeetingStateService
from app.services.transcript import preferred_segments

meetings_bp = Blueprint("meetings", __name__)


@meetings_bp.route("/meetings")
@login_required
def list_meetings():
    q = request.args.get("q") or ""
    rows = Meeting.query.order_by(Meeting.created_at.desc()).all()
    if q:
        needle = q.lower()
        matching_ids = {
            row.meeting_id
            for row in TranscriptSegment.query.all()
            if needle in (row.raw_text or "").lower()
            or needle in (row.punctuated_text or "").lower()
            or needle in (row.sensevoice_text or "").lower()
            or needle in (row.corrected_text or "").lower()
        }
        matching_ids.update(
            row.meeting_id for row in Decision.query.all() if needle in (row.text or "").lower()
        )
        matching_ids.update(
            row.meeting_id for row in ActionItem.query.all() if needle in (row.description or "").lower()
        )
        rows = [m for m in rows if needle in (m.name or "").lower() or m.id in matching_ids]
    jobs = (
        {row.meeting_id: row for row in MeetingImportJob.query.filter(MeetingImportJob.meeting_id.in_([m.id for m in rows])).all()}
        if rows
        else {}
    )
    return render_template("meetings/list.html", meetings=rows, jobs=jobs, q=q)


@meetings_bp.route("/meetings/new", methods=["GET", "POST"])
@login_required
def new_meeting():
    if request.method == "POST":
        consented = request.form.get("consent") in {"on", "1", "true", "yes"}
        if not consented:
            flash("请在开始前确认已告知与会人员录音及 AI 转写", "error")
            return render_template("meetings/new.html"), 400
        try:
            svc = MeetingService()
            m = svc.create(
                current_user,
                name=request.form.get("name"),
                expected_speakers=int(request.form.get("expected_speakers") or 4),
                meeting_type=request.form.get("meeting_type") or "其他",
            )
            svc.set_consent(m, True)
            client_id = request.form.get("client_id") or "web"
            svc.start(m, current_user, client_id=client_id)
            HostService().claim_host(m, client_id, user_id=current_user.id)
        except PermissionDenied:
            flash("没有权限创建会议", "error")
            return redirect(url_for("meetings.list_meetings"))
        return redirect(url_for("meetings.live", meeting_id=m.id))
    return render_template("meetings/new.html")


@meetings_bp.route("/meetings/import")
@login_required
def import_audio():
    return render_template("meetings/import.html")


@meetings_bp.route("/meetings/<meeting_id>/prepare", methods=["GET", "POST"])
@login_required
def prepare(meeting_id: str):
    svc = MeetingService()
    meeting = svc.get(meeting_id)
    if not meeting:
        flash("会议不存在", "error")
        return redirect(url_for("meetings.list_meetings"))
    svc.prepare(meeting)
    if request.method == "POST":
        consented = request.form.get("consent") in {"on", "1", "true", "yes"}
        svc.set_consent(meeting, consented)
        if not consented:
            flash("开始会议前必须确认已告知与会人员", "error")
            return render_template("meetings/prepare.html", meeting=meeting)
        try:
            client_id = request.form.get("client_id") or "web"
            svc.start(meeting, current_user, client_id=client_id)
            HostService().claim_host(meeting, client_id, user_id=current_user.id)
        except (ConsentRequired, PermissionDenied) as exc:
            flash(str(exc), "error")
            return render_template("meetings/prepare.html", meeting=meeting)
        return redirect(url_for("meetings.live", meeting_id=meeting.id))
    return render_template("meetings/prepare.html", meeting=meeting)


@meetings_bp.route("/meetings/<meeting_id>/live")
@login_required
def live(meeting_id: str):
    meeting = MeetingService().get(meeting_id)
    if not meeting:
        return redirect(url_for("meetings.list_meetings"))
    segs = preferred_segments(meeting.id, include_assistant=True)
    state = MeetingStateService().current(meeting.id)
    is_host = meeting.host_user_id == current_user.id or meeting.host_client_id is None
    return render_template(
        "meetings/live.html",
        meeting=meeting,
        segments=segs,
        state=state,
        is_host=is_host,
    )


@meetings_bp.route("/meetings/<meeting_id>")
@login_required
def detail(meeting_id: str):
    meeting = MeetingService().get(meeting_id)
    if not meeting:
        return redirect(url_for("meetings.list_meetings"))
    # A completed full-recording ASR pass supersedes human live captions, but
    # it never recreates the assistant's persisted replies. Keep those replies
    # in the attendee-facing post-meeting record as well as in exports.
    segs = preferred_segments(meeting.id, include_assistant=True)
    summary = (
        MeetingSummary.query.filter_by(meeting_id=meeting.id)
        .order_by(MeetingSummary.created_at.desc())
        .first()
    )
    decisions = Decision.query.filter_by(meeting_id=meeting.id).all()
    items = ActionItem.query.filter_by(meeting_id=meeting.id).all()
    import_job = MeetingImportJob.query.filter_by(meeting_id=meeting.id).first()
    store = current_app.extensions["audio_store"]
    master = store.master_path(meeting.id)
    return render_template(
        "meetings/detail.html",
        meeting=meeting,
        segments=segs,
        summary=summary,
        decisions=decisions,
        action_items=items,
        import_job=import_job,
        has_master=master.exists(),
    )

@meetings_bp.route("/meetings/<meeting_id>/audio")
@login_required
def meeting_audio(meeting_id: str):
    meeting = MeetingService().get(meeting_id)
    if not meeting:
        abort(404)
    require(current_user, "view")
    path = current_app.extensions["audio_store"].master_path(meeting_id)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="audio/wav", download_name="meeting_master.wav", as_attachment=False)


@meetings_bp.route("/meetings/<meeting_id>/export")
@login_required
def export_meeting(meeting_id: str):
    """Export an auditable transcript without exposing model-only fields."""
    meeting = MeetingService().get(meeting_id)
    if not meeting:
        abort(404)
    require(current_user, "view")
    fmt = (request.args.get("format") or "txt").lower()
    segments = preferred_segments(meeting.id, include_assistant=True)
    if fmt == "json":
        payload = {
            "meeting": {"id": meeting.id, "name": meeting.name, "status": meeting.status},
            "segments": [
                {
                    "id": seg.id,
                    "start_ms": seg.start_ms,
                    "end_ms": seg.end_ms,
                    "speaker": seg.speaker_label,
                    "raw_text": seg.raw_text,
                    "punctuated_text": seg.punctuated_text,
                    "sensevoice_text": seg.sensevoice_text,
                    "corrected_text": seg.corrected_text,
                    "final_text": seg.final_text or seg.display_text or seg.raw_text,
                    "source": seg.source,
                }
                for seg in segments
            ],
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        mimetype = "application/json"
        suffix = "json"
    else:
        lines = [f"# {meeting.name}", ""]
        for seg in segments:
            seconds = max(0, int(seg.start_ms or 0) // 1000)
            lines.append(f"[{seconds // 60:02d}:{seconds % 60:02d}] {seg.speaker_label}: {seg.final_text or seg.display_text or seg.raw_text}")
        body, mimetype, suffix = "\n".join(lines) + "\n", "text/plain", "txt"
    AuditService().record("meeting_export", current_user.id, target=meeting.id, extra={"format": suffix})
    return Response(
        body,
        mimetype=f"{mimetype}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="meeting-{meeting.id}.{suffix}"'},
    )
