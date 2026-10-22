"""Assemble sequential audio chunks into meeting_master.wav."""

from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from app.logging import get_logger
from app.media import resolve_ffmpeg
from app.storage.audio_store import AudioStore

log = get_logger("assembler")

# The AudioWorklet resamples its PCM16 spool to the same 16 kHz rate FunASR uses.
PCM_RATE = 16000
REALTIME_PCM_RATE = 16000
PCM_CHANNELS = 1
PCM_WIDTH = 2
COMPRESSED_SUFFIXES = {".webm", ".m4a", ".ogg", ".opus", ".mp3"}
REALTIME_PCM = "realtime.pcm"


def _read_pcm(path: Path) -> tuple[bytes, int, int, int]:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        with wave.open(str(path), "rb") as wf:
            return wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
    raise ValueError(f"refusing to treat {path.suffix} as raw PCM")


def _write_wav(path: Path, pcm: bytes, rate: int = 16000, channels: int = 1, sampwidth: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def wrap_realtime_pcm_bytes(data: bytes, dest: Path, *, rate: int = REALTIME_PCM_RATE) -> Path:
    """Wrap raw PCM16 mono bytes as a 16 kHz WAV for final FunASR."""
    if not data:
        raise ValueError("empty pcm")
    _write_wav(dest, data, rate=rate, channels=PCM_CHANNELS, sampwidth=PCM_WIDTH)
    return dest


def _wrap_realtime_pcm(store: AudioStore, meeting_id: str, dest: Path) -> bool:
    pcm_path = store.meeting_dir(meeting_id) / REALTIME_PCM
    if not pcm_path.exists():
        return False
    data = pcm_path.read_bytes()
    if not data:
        return False
    wrap_realtime_pcm_bytes(data, dest, rate=REALTIME_PCM_RATE)
    return True


def _silence_or_pcm_fallback(store: AudioStore, meeting_id: str, dest: Path) -> Path:
    if _wrap_realtime_pcm(store, meeting_id, dest):
        log.info("master fallback to realtime.pcm meeting=%s", meeting_id)
        return dest
    write_silence_wav(dest)
    log.warning("master fallback to silence wav meeting=%s", meeting_id)
    return dest


def _ffmpeg_decode(src: Path, dest: Path) -> bool:
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        log.warning("ffmpeg not found; cannot decode %s", src)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-ac",
        str(PCM_CHANNELS),
        "-ar",
        str(PCM_RATE),
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffmpeg decode failed src=%s err=%s", src, exc)
        return False
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace")[:500]
        log.warning("ffmpeg decode failed src=%s code=%s err=%s", src, proc.returncode, err)
        return False
    try:
        with wave.open(str(dest), "rb") as wf:
            if wf.getsampwidth() != PCM_WIDTH:
                return False
            wf.readframes(1)
    except wave.Error:
        log.warning("ffmpeg output is not a valid wav: %s", dest)
        return False
    return True


def _concat_wav_chunks(chunk_paths: list[Path]) -> tuple[bytes, int, int, int] | None:
    pcm_parts: list[bytes] = []
    rate, channels, width = PCM_RATE, PCM_CHANNELS, PCM_WIDTH
    for path in chunk_paths:
        if not path.exists():
            continue
        if path.suffix.lower() != ".wav":
            continue
        frames, r, c, w = _read_pcm(path)
        rate, channels, width = r, c, w
        pcm_parts.append(frames)
    if not pcm_parts:
        return None
    return b"".join(pcm_parts), rate, channels, width


def assemble_master(store: AudioStore, meeting_id: str, chunk_paths: list[Path]) -> Path:
    """Concatenate chunk files in order into meeting_master.wav.

    Prefer ffmpeg WebM/compressed concat (correct duration) when chunks exist.
    WAV chunks are concatenated via the wave module. realtime.pcm fallback is
    wrapped at the canonical 16 kHz realtime PCM rate. Never write compressed
    bytes into a WAV file.
    """
    master = store.master_path(meeting_id)
    existing = [p for p in chunk_paths if p.exists()]
    wav_paths = [p for p in existing if p.suffix.lower() == ".wav"]
    compressed = [p for p in existing if p.suffix.lower() in COMPRESSED_SUFFIXES]

    if compressed:
        suffix = compressed[0].suffix.lower() or ".webm"
        tmp_in: Path | None = None
        tmp_out: Path | None = None
        decoded = False
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
                tmp_in = Path(fh.name)
                for path in compressed:
                    fh.write(path.read_bytes())
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                tmp_out = Path(fh.name)
            if tmp_in is not None and tmp_out is not None and _ffmpeg_decode(tmp_in, tmp_out):
                master.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tmp_out, master)
                decoded = True
        finally:
            for p in (tmp_in, tmp_out):
                if p is not None:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
        if decoded:
            return master
        return _silence_or_pcm_fallback(store, meeting_id, master)

    if wav_paths:
        result = _concat_wav_chunks(wav_paths)
        if result:
            pcm, rate, channels, width = result
            _write_wav(master, pcm, rate=rate, channels=channels, sampwidth=width)
            return master

    return _silence_or_pcm_fallback(store, meeting_id, master)


def missing_sequences(received: list[int]) -> list[int]:
    if not received:
        return []
    lo, hi = min(received), max(received)
    have = set(received)
    return [i for i in range(lo, hi + 1) if i not in have]


def wav_duration_ms(path: Path) -> int:
    if not path.exists():
        return 0
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate() or 16000
        return int(frames / rate * 1000)


def write_silence_wav(path: Path, duration_ms: int = 500, rate: int = 16000) -> Path:
    n = int(rate * duration_ms / 1000)
    pcm = struct.pack("<" + "h" * n, *([0] * n))
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_wav(path, pcm, rate=rate)
    return path
