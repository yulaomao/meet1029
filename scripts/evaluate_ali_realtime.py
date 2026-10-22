#!/usr/bin/env python3
"""Replay a local AliMeeting Eval clip through the real streaming ASR provider.

This is a model-level real-time trace (40 ms PCM16 frames), not a browser or
Socket.IO end-to-end test. It records stable partials and rolling CER so model
quality is never inferred from the fake-provider pipeline tests.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import jiwer
import numpy as np
import soundfile as sf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from app.providers.funasr import FunASRStreamingProvider


DEFAULT_ROOT = ROOT / ".local" / "data" / "datasets" / "Eval_Ali"
INTERVAL = re.compile(
    r'intervals \[\d+\]:\s*xmin = ([0-9.]+)\s*xmax = ([0-9.]+)\s*text = "(.*?)"',
    re.DOTALL,
)
TIER_START = re.compile(r"^[ \t]*item \[\d+\]:[ \t]*$", re.MULTILINE)
TIER_NAME = re.compile(r'^\s*name = "(.*?)"\s*$', re.MULTILINE)


def normalize(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text or ""))


def read_intervals(path: Path, tier: str | None = None) -> list[tuple[float, float, str]]:
    source = path.read_text(encoding="utf-8")
    starts = list(TIER_START.finditer(source))
    blocks = [source[match.start() : starts[i + 1].start() if i + 1 < len(starts) else len(source)] for i, match in enumerate(starts)]
    if not blocks:
        blocks = [source]
    rows = []
    for block in blocks:
        name_match = TIER_NAME.search(block)
        name = name_match.group(1) if name_match else None
        if tier is not None and name != tier:
            continue
        for start, end, text in INTERVAL.findall(block):
            text = normalize(text.replace('""', '"'))
            if text:
                rows.append((float(start), float(end), text))
    if tier is not None and not any((TIER_NAME.search(block) and TIER_NAME.search(block).group(1) == tier) for block in blocks):
        raise ValueError(f"TextGrid tier not found: {tier}")
    return sorted(rows)


def reference_until(rows: list[tuple[float, float, str]], start: float, end: float) -> str:
    # TextGrid annotations are utterance intervals. Use only fully elapsed
    # intervals, avoiding credit for words whose audio has not played yet.
    return "".join(text for left, stop, text in rows if left >= start and stop <= end)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--recording", default="R8001_M8004_MS801")
    parser.add_argument("--track", choices=("far", "near"), default="far")
    parser.add_argument("--speaker", default="SPK8013", help="near-field speaker ID, e.g. SPK8013")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--channel", help="select one channel index; use 'mean' only after checking phase correlation")
    parser.add_argument("--tier", help="score against one TextGrid speaker tier, e.g. N_SPK8013")
    parser.add_argument("--all-reference-tiers", action="store_true", help="include every speaker tier in the reference")
    parser.add_argument("--fast", action="store_true", help="do not wait for real-time playback pacing")
    parser.add_argument("--report", type=Path, default=ROOT / ".local" / "evals" / "ali-realtime.json")
    args = parser.parse_args()

    recording = args.recording.removesuffix("_MS801")
    default_tier = f"N_{args.speaker}" if args.track == "near" else None
    audio_name = f"{recording}_N_{args.speaker}.wav" if args.track == "near" else f"{recording}_MS801.wav"
    track_dir = args.data_root / f"Eval_Ali_{args.track}"
    audio_path = track_dir / "audio_dir" / audio_name
    grid_path = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{recording}.TextGrid"
    if not audio_path.is_file() or not grid_path.is_file():
        raise FileNotFoundError(f"missing AliMeeting audio or TextGrid: {audio_path}, {grid_path}")
    info = sf.info(audio_path)
    if info.samplerate != 16000 or info.subtype != "PCM_16":
        raise ValueError(f"expected 16 kHz PCM16, got {info.samplerate} Hz {info.subtype}")
    if args.start < 0 or args.duration <= 0 or args.start + args.duration > info.duration:
        raise ValueError(f"clip range exceeds audio duration {info.duration:.3f}s")
    if args.channel is None:
        if info.channels > 1:
            raise ValueError("multichannel audio requires an explicit --channel index; inspect channel phase before using --channel mean")
        channel = 0
    else:
        channel = None if args.channel == "mean" else int(args.channel)
    if channel is not None and not 0 <= channel < info.channels:
        raise ValueError(f"channel must be between 0 and {info.channels - 1}")

    reference_tier = None if args.all_reference_tiers else (args.tier or default_tier)
    refs = read_intervals(grid_path, reference_tier)
    provider = FunASRStreamingProvider()
    provider.start_session()
    cold_started = time.perf_counter()
    provider._load()
    model_load_ms = round((time.perf_counter() - cold_started) * 1000, 1)
    frame_samples = 640  # 40 ms at 16 kHz, matching the browser audio worklet.
    total_samples = round(args.duration * info.samplerate)
    stable_updates = 0
    first_partial_audio_ms = None
    first_partial_speech_ms = None
    first_partial_wall_ms = None
    first_speech_at = next((left for left, _right, _text in refs if left >= args.start), None)
    last_stable = ""
    snapshots = []
    started = time.perf_counter()
    next_snapshot = 5.0

    with sf.SoundFile(audio_path) as wav:
        wav.seek(round(args.start * info.samplerate))
        sent_samples = 0
        while sent_samples < total_samples:
            count = min(frame_samples, total_samples - sent_samples)
            samples = wav.read(count, dtype="float32", always_2d=True)
            if samples.shape[0] == 0:
                break
            mono = samples[:, channel] if channel is not None else samples.mean(axis=1)
            pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            provider.feed_audio(pcm)
            sent_samples += len(samples)
            elapsed_audio = sent_samples / info.samplerate
            stable = provider.get_stable_prefix().strip()
            if stable and stable != last_stable:
                stable_updates += 1
                last_stable = stable
                if first_partial_audio_ms is None:
                    first_partial_audio_ms = round(elapsed_audio * 1000, 1)
                    first_partial_speech_ms = (
                        round(max(0.0, elapsed_audio - (first_speech_at - args.start)) * 1000, 1)
                        if first_speech_at is not None
                        else None
                    )
                    first_partial_wall_ms = round((time.perf_counter() - started) * 1000, 1)
            while elapsed_audio >= next_snapshot:
                reference = reference_until(refs, args.start, args.start + next_snapshot)
                hypothesis = normalize(stable)
                snapshots.append(
                    {
                        "audio_s": round(next_snapshot, 1),
                        "reference_chars": len(reference),
                        "stable_chars": len(hypothesis),
                        "rolling_cer": round(float(jiwer.cer(reference, hypothesis)), 4) if reference else None,
                    }
                )
                next_snapshot += 5.0
            if not args.fast:
                deadline = started + elapsed_audio
                delay = deadline - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)

    provider.flush()
    final = normalize(provider.get_final() or provider.get_stable_prefix())
    reference = reference_until(refs, args.start, args.start + sent_samples / info.samplerate)
    elapsed = time.perf_counter() - started
    report = {
        "dataset": f"AliMeeting Eval / {args.track}-field",
        "reference_tier": reference_tier or "all tiers (exploratory only; overlapping speech is not a valid single-stream reference)",
        "recording": audio_path.name,
        "channel_policy": "mean of all channels (explicit; phase must be validated)" if channel is None else f"channel {channel}",
        "start_s": args.start,
        "audio_s": round(sent_samples / info.samplerate, 3),
        "mode": "real-time 40 ms frames" if not args.fast else "fast replay",
        "device": provider.device,
        "model_load_ms": model_load_ms,
        "first_stable_partial_audio_ms": first_partial_audio_ms,
        "first_stable_partial_from_first_speech_ms": first_partial_speech_ms,
        "first_stable_partial_wall_ms": first_partial_wall_ms,
        "stable_partial_updates": stable_updates,
        "elapsed_wall_s": round(elapsed, 3),
        "real_time_factor": round(elapsed / max(sent_samples / info.samplerate, 1e-6), 4),
        "reference_chars": len(reference),
        "final_chars": len(final),
        "final_cer": round(float(jiwer.cer(reference, final)), 4) if reference else None,
        "snapshots": snapshots,
        "reference": reference,
        "hypothesis": final,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"reference", "hypothesis"}}, ensure_ascii=False, indent=2))
    print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
