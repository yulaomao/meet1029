"""Local VAD used to delimit ASR utterances.

The previous implementation called ``torch.hub`` at runtime, which silently
turned a local meeting product into a network-dependent one.  FSMN-VAD is
loaded through FunASR when it is installed locally; a deterministic energy VAD
is the safe offline fallback.
"""

from __future__ import annotations

import struct
import os

from app.providers.base import VADProvider


class FakeVAD(VADProvider):
    def is_speech(self, pcm16: bytes) -> bool:
        return bool(pcm16) and len(pcm16) >= 2


class EnergyVAD(VADProvider):
    def __init__(self, threshold: float | None = None):
        self.threshold = float(threshold if threshold is not None else os.environ.get("VAD_ENERGY_THRESHOLD", "50"))

    def is_speech(self, pcm16: bytes) -> bool:
        if not pcm16 or len(pcm16) < 2:
            return False
        n = len(pcm16) // 2
        samples = struct.unpack("<" + "h" * n, pcm16[: n * 2])
        rms = (sum(s * s for s in samples) / n) ** 0.5
        return rms > self.threshold


def build_vad() -> VADProvider:
    """Return an offline detector without downloading any model at runtime.

    The streaming Paraformer model still receives only speech frames.  FSMN-VAD
    is used in final transcription where FunASR can analyse the full waveform;
    energy VAD here keeps live endpointing predictable and dependency-free.
    """
    return EnergyVAD()
