"""Voiceprint enrollment: require real audio, derive embedding from bytes.

UI_DESIGN / SYSTEM_DESIGN: 30-60s microphone sample. Tests hash audio bytes via FakeEmbeddingProvider.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO

from app.web.extensions import db

MIN_SECONDS = 30.0
MAX_SECONDS = 60.0
RECOMMENDED_SECONDS = 60.0


class EnrollError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def wav_duration_seconds(data: bytes) -> float | None:
    if not data or len(data) < 44:
        return None
    if not data.startswith(b"RIFF"):
        return None
    try:
        import wave

        with wave.open(BytesIO(data), "rb") as wf:
            rate = wf.getframerate() or 1
            return wf.getnframes() / float(rate)
    except Exception:
        return None


def resolve_seconds(data: bytes, sample_seconds: float | None) -> float:
    duration = wav_duration_seconds(data)
    if duration is not None:
        return duration
    return float(sample_seconds or 0)


def embed_from_audio(provider, data: bytes, *, mime: str | None = None) -> list[float]:
    if hasattr(provider, "embed_audio"):
        try:
            return provider.embed_audio(data, mime=mime)
        except TypeError:
            return provider.embed_audio(data)
    digest = hashlib.sha256(data or b"").hexdigest()
    return provider.embed(digest)


def enroll_profile(profile, data: bytes, *, sample_seconds: float | None = None, embedding_provider=None, mime: str | None = None):
    if not data:
        raise EnrollError("missing audio")
    seconds = resolve_seconds(data, sample_seconds)
    if seconds < MIN_SECONDS:
        raise EnrollError("声纹登记需要 30～60 秒清晰语音")
    stored = min(seconds, MAX_SECONDS)
    profile.embedding = embed_from_audio(embedding_provider, data, mime=mime)
    profile.sample_seconds = stored
    profile.enrolled_at = datetime.now(timezone.utc)
    db.session.add(profile)
    db.session.commit()
    return profile
