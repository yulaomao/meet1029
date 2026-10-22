"""Master audio is served from meeting_master.wav, not silence.wav."""

from __future__ import annotations

import struct
import wave
from io import BytesIO
from pathlib import Path

from app.models import User
from app.services.meeting import MeetingService


def _wav() -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<" + "h" * 320, *([1234] * 320)))
    return buf.getvalue()


def test_detail_template_does_not_use_silence_wav():
    html = Path("app/web/templates/meetings/detail.html").read_text(encoding="utf-8")
    assert "silence.wav" not in html
    assert "meeting_audio" in html or "meetings.meeting_audio" in html


def test_master_audio_requires_login(client, app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="noauth")
        mid = m.id
    r = client.get("/meetings/" + mid + "/audio")
    assert r.status_code in (302, 401)


def test_master_audio_returns_meeting_master_bytes(app, auth_admin):
    wav = _wav()
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="with-audio")
        mid = m.id
        path = app.extensions["audio_store"].master_path(mid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wav)
    r = auth_admin.get("/meetings/" + mid + "/audio")
    assert r.status_code == 200
    assert r.data == wav
    assert r.mimetype == "audio/wav"
    r2 = auth_admin.get("/api/meetings/" + mid + "/master.wav")
    assert r2.status_code == 200
    assert r2.data == wav


def test_master_audio_missing_is_404(app, auth_admin):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="no-master")
        mid = m.id
    r = auth_admin.get("/meetings/" + mid + "/audio")
    assert r.status_code == 404
    page = auth_admin.get("/meetings/" + mid)
    assert page.status_code == 200
    assert b"silence.wav" not in page.data
