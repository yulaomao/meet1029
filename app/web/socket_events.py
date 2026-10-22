"""Socket.IO namespace /live."""

from __future__ import annotations

import time

from flask import current_app, request
from flask_socketio import emit, join_room, leave_room

from app.models import Meeting, TranscriptSegment
from app.services.meeting import MeetingService
from app.services.meeting_state import MeetingStateService
from app.web.extensions import db

NS = "/live"

# sid -> meeting_id (binary audio_frame has no JSON meeting_id)
SID_MEETING: dict[str, str] = {}


def _as_bytes(payload) -> bytes:
    if payload is None:
        return b""
    if isinstance(payload, memoryview):
        return payload.tobytes()
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    return b""


def _ints_or_bytes_list(raw) -> bytes:
    """Accept a list/tuple of ints, or a one-element list of bytes (polling)."""
    if not isinstance(raw, (list, tuple)):
        return b""
    if len(raw) == 1 and isinstance(raw[0], (bytes, bytearray, memoryview)):
        return _as_bytes(raw[0])
    try:
        return bytes(raw)
    except (TypeError, ValueError):
        return b""


def parse_audio_frame(data, meeting_id_from_session: str | None = None) -> tuple[str | None, bytes]:
    """Accept Socket.IO binary frames (bytes) or legacy JSON dicts."""
    meeting_id = meeting_id_from_session
    pcm = b""
    if isinstance(data, (bytes, bytearray, memoryview)):
        pcm = _as_bytes(data)
    elif isinstance(data, dict):
        meeting_id = data.get("meeting_id") or meeting_id
        raw = data.get("pcm") or data.get("frame") or b""
        if isinstance(raw, (list, tuple)):
            pcm = _ints_or_bytes_list(raw)
        else:
            pcm = _as_bytes(raw)
    elif isinstance(data, (list, tuple)):
        pcm = _ints_or_bytes_list(data)
    else:
        pcm = _as_bytes(data)
    return meeting_id, pcm


def store_pcm(meeting_id: str | None, pcm: bytes) -> None:
    """Keep in-memory buffers (tests) and append to the on-disk PCM spool."""
    if not meeting_id or not pcm:
        return
    buffers = current_app.extensions.setdefault("pcm_buffers", {})
    buffers.setdefault(meeting_id, bytearray()).extend(pcm)
    store = current_app.extensions.get("audio_store")
    if store is None:
        return
    path = store.meeting_dir(meeting_id) / "realtime.pcm"
    with path.open("ab") as fh:
        fh.write(pcm)


def transcript_final_payload(
    seg,
    *,
    text: str | None = None,
    speaker: str | None = None,
    utterance_id: int | None = None,
    turn_revision: int | None = None,
    sequence: int | None = None,
) -> dict:
    """Shared shape for live.js; utterance_id links partial/final revisions."""
    payload = {
        "meeting_id": seg.meeting_id,
        "id": seg.id,
        "text": text if text is not None else (seg.display_text or seg.raw_text or ""),
        "speaker": speaker if speaker is not None else (seg.speaker_label or "Speaker 1"),
        "start_ms": int(seg.start_ms or 0),
        "end_ms": int(seg.end_ms or 0),
    }
    if utterance_id is not None:
        payload["utterance_id"] = int(utterance_id)
    if turn_revision is not None:
        payload["turn_revision"] = int(turn_revision)
    if sequence is not None:
        payload["sequence"] = int(sequence)
    return payload


def register_socketio(socketio) -> None:
    @socketio.on("connect", namespace=NS)
    def on_connect():
        return True

    @socketio.on("disconnect", namespace=NS)
    def on_disconnect():
        SID_MEETING.pop(request.sid, None)

    @socketio.on("join_meeting", namespace=NS)
    def join_meeting(data):
        meeting_id = (data or {}).get("meeting_id")
        if not meeting_id:
            emit("error", {"message": "missing meeting_id"})
            return
        SID_MEETING[request.sid] = meeting_id
        join_room(meeting_id)
        emit("recording_status", {"meeting_id": meeting_id, "joined": True, "sid": request.sid})
        # A single catch-up on join is enough. Replaying the last 50 segments
        # every heartbeat causes needless DOM updates and visible line flicker.
        segs = (
            TranscriptSegment.query.filter_by(meeting_id=meeting_id)
            .order_by(TranscriptSegment.start_ms.asc(), TranscriptSegment.created_at.asc())
            .all()
        )
        for seg in segs[-50:]:
            emit("transcript_final", transcript_final_payload(seg))
        # The recognizer runs in a separate worker.  Replay its last persisted
        # spool backlog on join so the header does not remain at a placeholder
        # until the next microphone frame arrives.
        cursor = current_app.extensions.get("audio_store").read_json(meeting_id, "speech_cursor.json") or {}
        if isinstance(cursor, dict) and cursor.get("asr_metrics_at"):
            emit(
                "asr_metrics",
                {
                    "queue_lag_ms": int(cursor.get("asr_queue_lag_ms") or 0),
                    "processing_ms": int(cursor.get("asr_processing_ms") or 0),
                },
            )

    @socketio.on("leave_meeting", namespace=NS)
    def leave_meeting(data):
        meeting_id = (data or {}).get("meeting_id") or SID_MEETING.pop(request.sid, None)
        if meeting_id:
            leave_room(meeting_id)

    @socketio.on("heartbeat", namespace=NS)
    def heartbeat(data):
        meeting_id = (data or {}).get("meeting_id") or SID_MEETING.get(request.sid)
        meeting = db.session.get(Meeting, meeting_id) if meeting_id else None
        if meeting:
            MeetingService().heartbeat(meeting)
        emit("client_health", {"ok": True, "sid": request.sid})

    @socketio.on("audio_frame", namespace=NS)
    def audio_frame(data):
        # Models live in the speech worker. Web only spools PCM.
        meeting_id, pcm = parse_audio_frame(data, SID_MEETING.get(request.sid))
        store_pcm(meeting_id, pcm)

    @socketio.on("stop_ai", namespace=NS)
    def stop_ai(data):
        """Browser playback stops immediately; worker consumes this local marker."""
        meeting_id = (data or {}).get("meeting_id") or SID_MEETING.get(request.sid)
        turn_id = int((data or {}).get("turn_id") or 0)
        end_dialogue = bool((data or {}).get("end_dialogue"))
        if not meeting_id or (turn_id < 1 and not end_dialogue):
            return
        store = current_app.extensions.get("audio_store")
        if store is not None:
            store.write_json(
                meeting_id,
                "ai_control.json",
                {
                    "stop_turn_id": turn_id,
                    "end_dialogue": end_dialogue,
                    "requested_at": time.time(),
                },
            )

    @socketio.on("meeting_state_patch", namespace=NS)
    def meeting_state_patch(data):
        meeting_id = (data or {}).get("meeting_id") or SID_MEETING.get(request.sid)
        patch = (data or {}).get("patch") or {}
        if not meeting_id:
            return
        state = MeetingStateService().apply_patch(meeting_id, patch)
        emit("meeting_state_patch", {"meeting_id": meeting_id, "state": state.to_dict()}, to=meeting_id)
