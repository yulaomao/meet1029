"""assemble_master must decode compressed chunks, never treat them as PCM."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from app.storage.assembler import assemble_master, wav_duration_ms
from app.storage.audio_store import AudioStore


def _write_pcm_wav(path: Path, samples: list[int], rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return path


def _read_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        return wf.readframes(wf.getnframes())


def test_wav_chunks_still_assemble(tmp_path: Path):
    store = AudioStore(tmp_path)
    mid = "m-wav"
    src = store.source_dir(mid)
    paths = []
    expected = b""
    for i, val in enumerate((100, 200, 300)):
        samples = [val] * 16
        p = src / f"chunk_{i:06d}.wav"
        _write_pcm_wav(p, samples)
        paths.append(p)
        expected += struct.pack("<" + "h" * 16, *samples)
    master = assemble_master(store, mid, paths)
    assert master.exists()
    assert _read_wav_pcm(master) == expected
    with wave.open(str(master), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2


def test_junk_webm_falls_back_to_realtime_pcm(tmp_path: Path):
    store = AudioStore(tmp_path)
    mid = "m-junk"
    src = store.source_dir(mid)
    junk = src / "chunk_000000.webm"
    junk.write_bytes(b"WEBMJUNK" * 1000)
    known = struct.pack("<" + "h" * 64, *range(64))
    (store.meeting_dir(mid) / "realtime.pcm").write_bytes(known)
    master = assemble_master(store, mid, [junk])
    pcm = _read_wav_pcm(master)
    assert b"WEBMJUNK" not in pcm
    assert junk.read_bytes() not in pcm
    assert pcm == known
    with wave.open(str(master), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2


def test_junk_webm_without_pcm_writes_silence_not_garbage(tmp_path: Path):
    store = AudioStore(tmp_path)
    mid = "m-empty"
    src = store.source_dir(mid)
    junk = src / "chunk_000000.webm"
    junk.write_bytes(b"WEBMJUNK" * 1000)
    master = assemble_master(store, mid, [junk])
    pcm = _read_wav_pcm(master)
    assert b"WEBMJUNK" not in pcm
    assert junk.read_bytes() not in pcm
    assert pcm == bytes(len(pcm))
    assert len(pcm) > 0


def _encode_webm(src_wav: Path, dest: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    codecs = (
        ["-c:a", "libopus", "-b:a", "48k"],
        ["-c:a", "libvorbis"],
    )
    for extra in codecs:
        proc = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src_wav), *extra, str(dest)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
    return False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_webm_duration_matches_source_not_byte_ratio(tmp_path: Path):
    store = AudioStore(tmp_path)
    mid = "m-webm"
    src_dir = store.source_dir(mid)
    seconds = 1.0
    rate = 16000
    n = int(rate * seconds)
    samples = [int(12000 * math.sin(2 * math.pi * 440.0 * i / rate)) for i in range(n)]
    src_wav = tmp_path / "sine.wav"
    _write_pcm_wav(src_wav, samples, rate=rate)
    webm = src_dir / "chunk_000000.webm"
    if not _encode_webm(src_wav, webm):
        pytest.skip("ffmpeg could not encode wav to webm")
    webm_bytes = webm.read_bytes()
    master = assemble_master(store, mid, [webm])
    src_ms = wav_duration_ms(src_wav)
    master_ms = wav_duration_ms(master)
    wrong_ms = int(len(webm_bytes) / 32000 * 1000)
    assert abs(master_ms - src_ms) < 400
    assert abs(master_ms - wrong_ms) > 200
    pcm = _read_wav_pcm(master)
    assert webm_bytes not in pcm
    assert b"WEBMJUNK" not in pcm
