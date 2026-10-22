"""Voiceprint enroll API: require real audio; embedding derived from file bytes."""

from __future__ import annotations

import math
import struct
import wave
from io import BytesIO

from app.models import SpeakerProfile, User
from app.providers.embedding import FakeEmbeddingProvider
from app.web.extensions import db


def _wav(seconds: float, freq: float = 440.0) -> bytes:
    n = int(16000 * seconds)
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        samples = [int(8000 * math.sin(2 * math.pi * freq * i / 16000)) for i in range(n)]
        wf.writeframes(struct.pack("<" + "h" * n, *samples))
    return buf.getvalue()


def _profile(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        p = SpeakerProfile(user_id=admin.id, display_name=admin.name, embedding=[])
        db.session.add(p)
        db.session.commit()
        return p.id


def test_enroll_rejects_missing_audio(app, auth_admin):
    pid = _profile(app)
    r = auth_admin.post(f"/api/speaker-profiles/{pid}/enroll", json={"sample_seconds": 45})
    assert r.status_code == 400
    assert "missing" in (r.get_json() or {}).get("error", "").lower() or "audio" in (r.get_json() or {}).get("error", "").lower() or "声纹" in (r.get_json() or {}).get("error", "")


def test_enroll_rejects_short_audio(app, auth_admin):
    pid = _profile(app)
    wav = _wav(1.0)
    r = auth_admin.post(
        f"/api/speaker-profiles/{pid}/enroll",
        data={"sample_seconds": "60", "audio": (BytesIO(wav), "short.wav")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    err = (r.get_json() or {}).get("error", "")
    assert "30" in err or "声纹" in err


def test_enroll_accepts_wav_and_hashes_bytes(app, auth_admin):
    pid = _profile(app)
    wav_a = _wav(30.0, freq=440.0)
    wav_b = _wav(30.0, freq=880.0)
    assert wav_a != wav_b
    r = auth_admin.post(
        f"/api/speaker-profiles/{pid}/enroll",
        data={"audio": (BytesIO(wav_a), "enroll.wav")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["sample_seconds"] >= 30
    with app.app_context():
        p = db.session.get(SpeakerProfile, pid)
        assert p.enrolled_at is not None
        assert p.embedding
        expected = FakeEmbeddingProvider().embed_audio(wav_a)
        assert p.embedding == expected
        other = FakeEmbeddingProvider().embed_audio(wav_b)
        assert p.embedding != other
        # must not be the old seconds-string stub
        stub = FakeEmbeddingProvider().embed(f"enroll-{p.user_id}-30.0")
        assert p.embedding != stub
