"""PCM16 framing: downsample to 16 kHz mono PCM16 ~100 ms frames.

A 100 ms frame at 16 kHz mono 16-bit is 1600 samples * 2 bytes = 3200 bytes.
"""

from __future__ import annotations

import array
import math
import struct


PCM_SAMPLE_RATE = 16000
FRAME_MS = 100
SAMPLES_PER_FRAME = PCM_SAMPLE_RATE * FRAME_MS // 1000  # 1600
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2  # 3200

def downsample_to_16k_mono(samples: list[float] | array.array, in_rate: int, channels: int = 1) -> list[int]:
    """Naive but deterministic downsample + mixdown to 16 kHz mono int16."""
    if channels > 1:
        mono = []
        for i in range(0, len(samples), channels):
            chunk = samples[i : i + channels]
            mono.append(sum(chunk) / max(len(chunk), 1))
        samples = mono
    if in_rate == PCM_SAMPLE_RATE:
        pcm = [_clamp_i16(s) for s in samples]
        return pcm
    ratio = in_rate / PCM_SAMPLE_RATE
    out_len = int(len(samples) / ratio)
    out = []
    for i in range(out_len):
        src = i * ratio
        left = int(math.floor(src))
        right = min(left + 1, len(samples) - 1)
        frac = src - left
        val = samples[left] * (1 - frac) + samples[right] * frac
        out.append(_clamp_i16(val))
    return out


def _clamp_i16(val: float) -> int:
    if val > 32767:
        return 32767
    if val < -32768:
        return -32768
    # Accept already-int16 or float -1..1
    if -1.5 < val < 1.5 and not isinstance(val, int):
        val = val * 32767.0
    return int(val)


def frames_from_pcm16(pcm: list[int] | bytes, frame_ms: int = FRAME_MS) -> list[bytes]:
    if isinstance(pcm, bytes):
        samples = list(struct.unpack("<" + "h" * (len(pcm) // 2), pcm[: len(pcm) - (len(pcm) % 2)]))
    else:
        samples = list(pcm)
    n = PCM_SAMPLE_RATE * frame_ms // 1000
    frames = []
    for i in range(0, len(samples), n):
        chunk = samples[i : i + n]
        if len(chunk) < n:
            chunk = chunk + [0] * (n - len(chunk))
        frames.append(struct.pack("<" + "h" * n, *chunk))
    return frames


def encode_pcm16(samples: list[int]) -> bytes:
    return struct.pack("<" + "h" * len(samples), *[_clamp_i16(s) for s in samples])


def decode_pcm16(data: bytes) -> list[int]:
    n = len(data) // 2
    return list(struct.unpack("<" + "h" * n, data[: n * 2]))
