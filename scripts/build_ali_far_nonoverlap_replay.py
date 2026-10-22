#!/usr/bin/env python3
"""Assemble clear far-field turns into a non-overlapping live-pipeline replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_ali_far_nonoverlap import read_tiers, resolve_far_audio, select_nonoverlap_turns


SAMPLE_RATE = 16000
PAUSE_S = 4.0
LEAD_S = 0.12
TIER_NAME = "N_SPK9999"
OUTPUT_RECORDING = "ali_far_nonoverlap"
PROCESSING_FRAME_SAMPLES = 1600  # 100 ms at 16 kHz, matching SpeechPipeline


def parse_clip(value: str) -> tuple[str, float, float]:
    try:
        recording, start, duration = value.rsplit(":", 2)
        return recording, float(start), float(duration)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("clip must be RECORDING:START_SECONDS:DURATION_SECONDS") from exc


def textgrid_escape(value: str) -> str:
    return value.replace('"', '""')


def pad_to_processing_frame(waveform: np.ndarray) -> np.ndarray:
    """Pad mono PCM16 samples to the service's 100 ms processing boundary."""
    padding = (-len(waveform)) % PROCESSING_FRAME_SAMPLES
    return np.pad(waveform, (0, padding)) if padding else waveform


def write_textgrid(path: Path, total_s: float, turns: list[dict]) -> None:
    intervals = []
    cursor_s = 0.0
    for turn in turns:
        if turn["replay_start_s"] > cursor_s:
            intervals.append((cursor_s, turn["replay_start_s"], ""))
        intervals.append((turn["replay_start_s"], turn["replay_end_s"], turn["reference"]))
        cursor_s = turn["replay_end_s"]
    if total_s > cursor_s:
        intervals.append((cursor_s, total_s, ""))
    lines = [
        'File type = "ooTextFile"',
        'Object class = "TextGrid"',
        "",
        "xmin = 0",
        f"xmax = {total_s:.6f}",
        "tiers? <exists>",
        "size = 1",
        "item []:",
        "    item [1]:",
        '        class = "IntervalTier"',
        f'        name = "{TIER_NAME}"',
        "        xmin = 0",
        f"        xmax = {total_s:.6f}",
        f"        intervals: size = {len(intervals)}",
    ]
    for index, (left, right, text) in enumerate(intervals, start=1):
        lines.extend(
            [
                f"        intervals [{index}]:",
                f"            xmin = {left:.6f}",
                f"            xmax = {right:.6f}",
                f'            text = "{textgrid_escape(text)}"',
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--clip",
        type=parse_clip,
        action="append",
        required=True,
        help="source clip as RECORDING:START_SECONDS:DURATION_SECONDS; may be repeated",
    )
    parser.add_argument("--data-root", type=Path, default=Path(".local/data/datasets/Eval_Ali"))
    parser.add_argument("--output-root", type=Path, default=Path(".local/evals/ali-far-nonoverlap-live-dataset"))
    parser.add_argument("--channel", type=int, default=3)
    parser.add_argument("--minimum-duration", type=float, default=1.5)
    args = parser.parse_args()

    output_audio = args.output_root / "Eval_Ali_far" / "audio_dir" / f"{OUTPUT_RECORDING}_MS801.wav"
    output_grid = args.output_root / "Eval_Ali_far" / "textgrid_dir" / f"{OUTPUT_RECORDING}.TextGrid"
    output_audio.parent.mkdir(parents=True, exist_ok=True)
    output_grid.parent.mkdir(parents=True, exist_ok=True)

    audio_parts: list[np.ndarray] = []
    replay_turns: list[dict] = []
    cursor_samples = 0
    pause_samples = round(PAUSE_S * SAMPLE_RATE)
    lead_samples = round(LEAD_S * SAMPLE_RATE)
    silence = np.zeros(0, dtype="<i2")

    for recording, start_s, duration_s in args.clip:
        audio_dir = args.data_root / "Eval_Ali_far" / "audio_dir"
        audio_path, meeting_id = resolve_far_audio(audio_dir, recording)
        info = sf.info(audio_path)
        if info.samplerate != SAMPLE_RATE or info.subtype != "PCM_16":
            raise ValueError(f"expected 16 kHz PCM16 audio: {audio_path}")
        if not 0 <= args.channel < info.channels:
            raise ValueError(f"channel {args.channel} is invalid for {audio_path.name}")
        if start_s < 0 or duration_s <= 0 or start_s + duration_s > info.duration:
            raise ValueError(f"clip range exceeds duration of {audio_path.name}")
        grid_path = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{meeting_id}.TextGrid"
        selected = select_nonoverlap_turns(
            read_tiers(grid_path),
            start_s=start_s,
            end_s=start_s + duration_s,
            minimum_duration_s=args.minimum_duration,
        )
        with sf.SoundFile(audio_path) as audio:
            for item in selected:
                audio.seek(round(item["start_s"] * SAMPLE_RATE))
                count = round(item["duration_s"] * SAMPLE_RATE)
                samples = audio.read(count, dtype="int16", always_2d=True)[:, args.channel]
                if len(samples) != count:
                    continue
                if not audio_parts:
                    audio_parts.append(silence if len(silence) else np.zeros(lead_samples, dtype="<i2"))
                    cursor_samples = len(audio_parts[-1])
                else:
                    audio_parts.append(np.zeros(pause_samples, dtype="<i2"))
                    cursor_samples += pause_samples
                turn = dict(item)
                turn.update(
                    {
                        "source_recording": audio_path.name,
                        "source_channel": args.channel,
                        "replay_start_s": cursor_samples / SAMPLE_RATE,
                        "replay_end_s": (cursor_samples + count) / SAMPLE_RATE,
                    }
                )
                replay_turns.append(turn)
                audio_parts.append(samples.astype("<i2", copy=False))
                cursor_samples += count
        if selected:
            audio_parts.append(np.zeros(pause_samples, dtype="<i2"))
            cursor_samples += pause_samples

    if not replay_turns:
        raise RuntimeError("no non-overlapping turns selected for the live replay")
    waveform = np.concatenate(audio_parts)
    # SpeechPipeline consumes 100 ms PCM blocks. Pad the generated WAV to that
    # boundary so the live replay never leaves an unprocessable tail fragment.
    waveform = pad_to_processing_frame(waveform)
    total_s = len(waveform) / SAMPLE_RATE
    sf.write(output_audio, waveform, SAMPLE_RATE, subtype="PCM_16")
    write_textgrid(output_grid, total_s, replay_turns)
    metadata = {
        "synthetic_recording": OUTPUT_RECORDING,
        "channel": args.channel,
        "turn_count": len(replay_turns),
        "duration_s": round(total_s, 3),
        "pause_s": PAUSE_S,
        "lead_s": LEAD_S,
        "turns": replay_turns,
        "use": "Replay this WAV through replay_ali_live_pipeline.py with tier N_SPK9999. This preserves each source turn while eliminating cross-speaker overlap and adding endpoint pauses.",
    }
    metadata_path = args.output_root / "source-turns.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in metadata.items() if key != "turns"}, ensure_ascii=False, indent=2))
    print(f"audio: {output_audio.resolve()}")
    print(f"textgrid: {output_grid.resolve()}")
    print(f"metadata: {metadata_path.resolve()}")


if __name__ == "__main__":
    main()
