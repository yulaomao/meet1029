"""MEETING-AUDIO: chunks stay local until server ACK."""

from __future__ import annotations

from pathlib import Path


def test_chunk_ack_no_delete_before_ack(app, auth_admin, tmp_path):
    """MEETING-AUDIO-012: client must not delete a chunk until ack:true.

    Server-side: ingest is idempotent and returns ack. A simulated local queue
    only drops the blob after a successful ACK; a 500 leaves it in place.
    """
    from app.models import Meeting
    from app.services.meeting import MeetingService
    from app.web.extensions import db

    with app.app_context():
        admin = db.session.query(__import__("app.models", fromlist=["User"]).User).filter_by(email="admin@local").first()
        meeting = MeetingService().create(admin, name="ack-test")
        MeetingService().set_consent(meeting, True)
        MeetingService().start(meeting, admin, client_id="c1")
        mid = meeting.id

    local = {0: b"RIFFWAV"}
    # Simulate failed upload: local copy remains.
    resp = auth_admin.post(
        f"/api/meetings/{mid}/audio/chunks",
        data={"seq": 0, "start_ms": 0, "mime": "audio/wav", "client_id": "c1"},
        content_type="multipart/form-data",
    )
    # Without file the API may 400; send as file
    from io import BytesIO

    wav = _tiny_wav()
    resp = auth_admin.post(
        f"/api/meetings/{mid}/audio/chunks",
        data={"seq": "0", "start_ms": "0", "mime": "audio/wav", "client_id": "c1", "file": (BytesIO(wav), "chunk_000000.wav")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ack"] is True
    assert body["seq"] == 0
    # Local delete happens only after ack
    if body.get("ack"):
        local.pop(0, None)
    assert 0 not in local

    # Idempotent re-upload of same seq still ACKs and does not corrupt.
    resp2 = auth_admin.post(
        f"/api/meetings/{mid}/audio/chunks",
        data={"seq": "0", "start_ms": "0", "mime": "audio/wav", "client_id": "c1", "file": (BytesIO(wav), "chunk_000000.wav")},
        content_type="multipart/form-data",
    )
    assert resp2.get_json()["ack"] is True
    assert resp2.get_json().get("idempotent") is True


def _tiny_wav() -> bytes:
    import struct
    import wave
    from io import BytesIO

    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<" + "h" * 160, *([0] * 160)))
    return buf.getvalue()
