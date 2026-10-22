#!/usr/bin/env python3
"""Evaluate a small, local AISHELL-4 meeting slice without downloading a corpus.

The default asset is one 38-minute, eight-channel Mandarin meeting recording
plus its official TextGrid and RTTM annotations.  The script samples four
widely separated windows, runs the configured local streaming FunASR path, and
prints a JSON report containing CER and real-time factor.  It intentionally
does not upload audio or contact any service.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import jiwer

from app.providers.funasr import FunASRStreamingProvider


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "data" / "evaluation" / "aishell4_s_r004s03c01" / "test"
_INTERVAL = re.compile(
    r"intervals \[\d+\]:\s*xmin = ([0-9.]+)\s*xmax = ([0-9.]+)\s*text = \"(.*?)\"",
    re.DOTALL,
)


def normalize(text: str) -> str:
    text = re.sub(r"<[^>]+>|[&%]", "", text or "")
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text))


def textgrid_intervals(path: Path) -> list[tuple[float, float, str]]:
    rows = []
    for start, end, text in _INTERVAL.findall(path.read_text(encoding="utf-8")):
        cleaned = normalize(text)
        if cleaned:
            rows.append((float(start), float(end), cleaned))
    return rows


def rttm_speakers(path: Path) -> list[str]:
    speakers = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        cols = line.split()
        if len(cols) >= 8 and cols[0] == "SPEAKER":
            speakers.add(cols[7])
    return sorted(speakers)


def reference_for(intervals: list[tuple[float, float, str]], start: float, end: float) -> str:
    return "".join(text for left, right, text in intervals if right > start and left < end)


def extract_window(source: Path, start: float, duration: float, output: Path) -> None:
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-t", str(duration),
        # The recording is one eight-channel FLAC stream.  ffmpeg's portable
        # downmix works across its macOS builds, unlike deprecated map_channel.
        "-i", str(source), "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "wav", str(output),
    ]
    subprocess.run(command, check=True, timeout=90)


def streaming_transcribe(wav_path: Path, provider: FunASRStreamingProvider) -> str:
    provider.start_session()
    with wave.open(str(wav_path), "rb") as audio:
        assert audio.getframerate() == 16000 and audio.getnchannels() == 1
        # Feed one second at a time; provider itself cuts into its validated
        # 480-ms Paraformer stride and exercises its live hypothesis merger.
        while data := audio.readframes(16000):
            provider.feed_audio(data)
    provider.flush()
    return normalize(provider.get_final())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--windows", type=int, default=4)
    args = parser.parse_args()
    wav = args.data_dir / "wav" / "S_R004S03C01.flac"
    grid = args.data_dir / "TextGrid" / "S_R004S03C01.TextGrid"
    rttm = args.data_dir / "TextGrid" / "S_R004S03C01.rttm"
    for path in (wav, grid, rttm):
        if not path.exists():
            raise FileNotFoundError(path)

    intervals = textgrid_intervals(grid)
    duration = 2327.119
    # Chosen from speech-bearing regions across the meeting rather than the
    # trailing silence at the end of the recording.
    anchors = [20.0, 600.0, 1200.0, 1800.0]
    if args.windows <= len(anchors):
        positions = anchors[: args.windows]
    else:
        positions = anchors + [round(i * (duration - args.seconds) / (args.windows - 1), 3) for i in range(4, args.windows)]
    results = []
    provider = FunASRStreamingProvider()
    with tempfile.TemporaryDirectory(prefix="aishell4-eval-") as tmp:
        temp = Path(tmp)
        for index, start in enumerate(positions):
            clip = temp / f"window-{index}.wav"
            extract_window(wav, start, args.seconds, clip)
            reference = reference_for(intervals, start, start + args.seconds)
            began = time.perf_counter()
            hypothesis = streaming_transcribe(clip, provider)
            elapsed = time.perf_counter() - began
            results.append(
                {
                    "start_s": start,
                    "duration_s": args.seconds,
                    "reference_chars": len(reference),
                    "hypothesis_chars": len(hypothesis),
                    "cer": round(float(jiwer.cer(reference, hypothesis)), 4) if reference else None,
                    "elapsed_s": round(elapsed, 3),
                    "rtf": round(elapsed / args.seconds, 4),
                    "reference": reference,
                    "hypothesis": hypothesis,
                }
            )
    cers = [row["cer"] for row in results if row["cer"] is not None]
    print(json.dumps({
        "dataset": "AISHELL-4 test/S_R004S03C01",
        "audio_duration_s": duration,
        "official_rttm_speakers": rttm_speakers(rttm),
        "official_annotated_intervals": len(intervals),
        "sampled_audio_s": args.seconds * args.windows,
        "mean_cer": round(sum(cers) / len(cers), 4) if cers else None,
        "mean_rtf": round(sum(row["rtf"] for row in results) / len(results), 4),
        "windows": results,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
