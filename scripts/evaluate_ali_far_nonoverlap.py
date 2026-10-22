#!/usr/bin/env python3
"""Score far-field recognition on fully annotated, non-overlapping speech turns.

This is an acoustic diagnostic, not the live-pipeline acceptance replay: it
feeds complete reference-bounded turns to the real local recognizers so the
single-stream hypothesis can be compared without concatenating overlapping
speaker references.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import jiwer
import numpy as np
import soundfile as sf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.providers.funasr import FunASRStreamingProvider, SenseVoiceRefiner  # noqa: E402
from scripts.evaluate_ali_realtime import normalize, read_intervals  # noqa: E402


TIER_START = re.compile(r"^[ \t]*item \[\d+\]:[ \t]*$", re.MULTILINE)
TIER_NAME = re.compile(r'^\s*name = "(.*?)"\s*$', re.MULTILINE)
MIC_SUFFIX = re.compile(r"_MS\d+$")


def resolve_far_audio(audio_dir: Path, recording: str) -> tuple[Path, str]:
    """Resolve meeting ID and actual MS microphone suffix from the Eval split."""
    prefix = MIC_SUFFIX.sub("", recording)
    if MIC_SUFFIX.search(recording):
        path = audio_dir / f"{recording}.wav"
        if not path.is_file():
            raise FileNotFoundError(path)
        return path, prefix
    matches = sorted(audio_dir.glob(f"{prefix}_MS*.wav"))
    if len(matches) != 1:
        raise ValueError(f"expected one far-field audio for {prefix}, found {[p.name for p in matches]}")
    return matches[0], prefix


def read_tiers(path: Path) -> dict[str, list[tuple[float, float, str]]]:
    source = path.read_text(encoding="utf-8")
    starts = list(TIER_START.finditer(source))
    tiers: dict[str, list[tuple[float, float, str]]] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(source)
        block = source[match.start() : end]
        tier_name = TIER_NAME.search(block)
        if not tier_name:
            continue
        name = tier_name.group(1)
        tiers[name] = read_intervals(path, name)
    return tiers


def select_nonoverlap_turns(
    tiers: dict[str, list[tuple[float, float, str]]],
    *,
    start_s: float,
    end_s: float,
    minimum_duration_s: float,
) -> list[dict]:
    selected = []
    for speaker, intervals in tiers.items():
        if not speaker.startswith("N_SPK"):
            continue
        for left, right, text in intervals:
            if left < start_s or right > end_s or right - left < minimum_duration_s:
                continue
            overlaps = any(
                other_speaker != speaker
                and max(left, other_left) < min(right, other_right)
                for other_speaker, other_intervals in tiers.items()
                if other_speaker.startswith("N_SPK")
                for other_left, other_right, _other_text in other_intervals
            )
            if overlaps:
                continue
            selected.append(
                {
                    "speaker_tier": speaker,
                    "start_s": left,
                    "end_s": right,
                    "duration_s": right - left,
                    "reference": text,
                }
            )
    return sorted(selected, key=lambda row: (row["start_s"], row["speaker_tier"]))


def transcribe_streaming(provider: FunASRStreamingProvider, pcm16: bytes) -> str:
    provider.reset()
    frame_bytes = 640 * 2
    for offset in range(0, len(pcm16), frame_bytes):
        provider.feed_audio(pcm16[offset : offset + frame_bytes])
    provider.flush()
    return normalize(provider.get_final() or provider.get_partial())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT / ".local" / "data" / "datasets" / "Eval_Ali")
    parser.add_argument("--recording", default="R8001_M8004_MS801")
    parser.add_argument("--channel", type=int, default=3)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--minimum-duration", type=float, default=1.5)
    parser.add_argument("--sensevoice", action="store_true", help="also score local SenseVoice-Small on each isolated turn")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / ".local" / "evals" / "ali-far-nonoverlap.json",
    )
    args = parser.parse_args()

    audio_dir = args.data_root / "Eval_Ali_far" / "audio_dir"
    audio_path, recording = resolve_far_audio(audio_dir, args.recording)
    grid_path = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{recording}.TextGrid"
    info = sf.info(audio_path)
    if info.samplerate != 16000 or info.subtype != "PCM_16":
        raise ValueError(f"expected 16 kHz PCM16 audio, got {info.samplerate} Hz {info.subtype}")
    if not 0 <= args.channel < info.channels:
        raise ValueError(f"channel must be between 0 and {info.channels - 1}")
    if args.start < 0 or args.duration <= 0 or args.start + args.duration > info.duration:
        raise ValueError(f"requested clip exceeds recording duration {info.duration:.3f}s")

    tiers = read_tiers(grid_path)
    turns = select_nonoverlap_turns(
        tiers,
        start_s=args.start,
        end_s=args.start + args.duration,
        minimum_duration_s=args.minimum_duration,
    )
    if not turns:
        raise RuntimeError("no complete non-overlapping speaker intervals match the requested clip")

    provider = FunASRStreamingProvider()
    warm_started = time.perf_counter()
    provider._load()
    sensevoice = SenseVoiceRefiner() if args.sensevoice else None
    if sensevoice:
        sensevoice._load()
    warmup_ms = round((time.perf_counter() - warm_started) * 1000, 1)

    with sf.SoundFile(audio_path) as audio:
        for row in turns:
            frame_start = round(row["start_s"] * info.samplerate)
            frame_count = round(row["duration_s"] * info.samplerate)
            audio.seek(frame_start)
            samples = audio.read(frame_count, dtype="float32", always_2d=True)[:, args.channel]
            pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            row["paraformer_text"] = transcribe_streaming(provider, pcm16)
            row["paraformer_cer"] = round(float(jiwer.cer(row["reference"], row["paraformer_text"])), 4)
            if sensevoice:
                row["sensevoice_text"] = normalize(sensevoice.transcribe_pcm(pcm16))
                row["sensevoice_cer"] = round(float(jiwer.cer(row["reference"], row["sensevoice_text"])), 4)

    def aggregate(text_key: str) -> dict:
        reference = "".join(row["reference"] for row in turns)
        hypothesis = "".join(row.get(text_key, "") for row in turns)
        edits = jiwer.process_characters(reference, hypothesis)
        return {
            "reference_chars": len(reference),
            "hypothesis_chars": len(hypothesis),
            "insertions": edits.insertions,
            "deletions": edits.deletions,
            "substitutions": edits.substitutions,
            "cer": round(float(jiwer.cer(reference, hypothesis)), 4),
        }

    report = {
        "dataset": "AliMeeting Eval / far-field isolated non-overlapping turns",
        "recording": audio_path.name,
        "channel": args.channel,
        "requested_start_s": args.start,
        "requested_duration_s": args.duration,
        "minimum_turn_duration_s": args.minimum_duration,
        "mode": "complete annotation-bounded turns; model-level diagnostic, not live-pipeline score",
        "device": provider.device,
        "model_warmup_ms": warmup_ms,
        "eligible_turn_count": len(turns),
        "eligible_speech_duration_s": round(sum(row["duration_s"] for row in turns), 3),
        "eligible_reference_chars": sum(len(row["reference"]) for row in turns),
        "paraformer": aggregate("paraformer_text"),
        "sensevoice": aggregate("sensevoice_text") if sensevoice else None,
        "turns": turns,
        "limitations": [
            "Scores only complete annotated intervals with no temporal overlap from another speaker tier.",
            "Audio is cropped at reference boundaries and sent through real local models without live VAD/endpointing.",
            "This diagnostic isolates far-field acoustic recognition; it does not replace the real-time Socket.IO replay.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "turns"}, ensure_ascii=False, indent=2))
    print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
