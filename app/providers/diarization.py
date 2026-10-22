"""Diarization providers. Sortformer is a Mac MLX stub; clustering and fake work on Linux."""

from __future__ import annotations

import sys

from app.exceptions import PlatformNotSupported
from app.providers.base import DiarizationProvider


class FakeDiarizationProvider(DiarizationProvider):
    """Sticky single-speaker stub.

    The old implementation cycled Speaker 1/2 every 8 frames, which labeled one
    human as two people in live meetings. Always report Speaker 1; the pipeline
    StickySpeakerAssigner is the live label source of truth.
    """

    def __init__(self, n_speakers: int = 1):
        self.n_speakers = max(1, int(n_speakers or 1))
        self._frames = 0

    def feed_audio(self, pcm16: bytes) -> None:
        self._frames += 1

    def current_speakers(self) -> list[dict]:
        return [{"speaker": "Speaker 1", "cluster_id": "0", "probability": 0.9}]

    def reset(self) -> None:
        self._frames = 0


class EmbeddingClusterProvider(DiarizationProvider):
    """Online clustering fallback. Sticky Speaker 1 until a real embedding model exists."""

    def __init__(self):
        self._segments: list[dict] = []

    def feed_audio(self, pcm16: bytes) -> None:
        self._segments.append({"bytes": len(pcm16 or b"")})

    def current_speakers(self) -> list[dict]:
        return [{"speaker": "Speaker 1", "cluster_id": "0", "probability": 0.6}]

    def reset(self) -> None:
        self._segments.clear()


class SortformerProvider(DiarizationProvider):
    """Sortformer Streaming v2.1 via MLX. Mac only. Max 4 speaker slots."""

    def __init__(self):
        if sys.platform != "darwin":
            raise PlatformNotSupported("SortformerProvider", sys.platform)
        self._frames = 0

    def feed_audio(self, pcm16: bytes) -> None:
        self._frames += 1

    def current_speakers(self) -> list[dict]:
        return [{"speaker": "Speaker 1", "cluster_id": "0", "probability": 0.8}]

    def reset(self) -> None:
        self._frames = 0


class PyannoteProvider(DiarizationProvider):
    """Optional pyannote Community-1 final speaker check."""

    def __init__(self):
        self._ready = False
        try:
            import pyannote.audio  # noqa: F401

            self._ready = True
        except ImportError:
            self._ready = False

    def feed_audio(self, pcm16: bytes) -> None:
        return None

    def current_speakers(self) -> list[dict]:
        return []

    def reset(self) -> None:
        return None
