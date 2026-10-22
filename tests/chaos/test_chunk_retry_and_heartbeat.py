from __future__ import annotations

import struct
import wave
from datetime import datetime, timedelta, timezone
from io import BytesIO

from app.models import Meeting
from app.models.enums import MeetingStatus
from app.services.health import HealthService
from app.services.meeting import MeetingService
from app.web.extensions import db


def _wav() -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<hh", 0, 0))
    return buf.getvalue()


def test_chunk_500_then_success_master_complete(app, auth_admin, monkeypatch):
    r = auth_admin.post("/api/meetings", json={"name": "chaos"})
    mid = r.get_json()["id"]
    auth_admin.post(f"/api/meetings/{mid}/start", json={"consent": True, "client_id": "h"})
    wav = _wav()

    class Boom(Exception):
        pass

    attempts = {"n": 0}
    from app.services import audio as audio_mod
    orig = audio_mod.AudioService.ingest_chunk

    def flaky(self, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise Boom("simulated 500")
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(audio_mod.AudioService, "ingest_chunk", flaky)
    # First call fails at service; API would 500. Then succeed.
    try:
        auth_admin.post(
            f"/api/meetings/{mid}/audio/chunks",
            data={"seq": "0", "mime": "audio/wav", "client_id": "h", "file": (BytesIO(wav), "c.wav")},
            content_type="multipart/form-data",
        )
    except Boom:
        pass
    monkeypatch.setattr(audio_mod.AudioService, "ingest_chunk", orig)
    ok = auth_admin.post(
        f"/api/meetings/{mid}/audio/chunks",
        data={"seq": "0", "mime": "audio/wav", "client_id": "h", "file": (BytesIO(wav), "c.wav")},
        content_type="multipart/form-data",
    )
    assert ok.get_json()["ack"] is True
    done = auth_admin.post(f"/api/meetings/{mid}/audio/complete", json={"client_id": "h", "force": True})
    assert done.get_json()["complete"] is True
    assert done.get_json()["master"]


def test_missing_heartbeat_health_degraded(app):
    with app.app_context():
        admin = db.session.query(__import__("app.models", fromlist=["User"]).User).filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="no-hb")
        MeetingService().set_consent(m, True)
        MeetingService().start(m, admin, client_id="x")
        m.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        db.session.commit()
        report = HealthService(app.extensions["providers"]).check()
        assert report["status"] == "degraded"
        assert report["degraded_meetings"]
