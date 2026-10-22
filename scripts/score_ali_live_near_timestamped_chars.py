#!/usr/bin/env python3
"""Score stored near-field live transcript text on clean target-speaker turns.

SenseVoice timestamps are used only to place live final characters in time.
TextGrid overlap regions and text that cannot be placed natively are reported
explicitly; this is a diagnostic, not a replacement for the full CER score.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from scripts.diagnose_ali_final_alignment import intervals_for_recording
from scripts.evaluate_ali_realtime import normalize
from scripts.score_ali_far_timestamped_chars import (
    clean_reference_turns,
    score_turns,
    timestamped_characters,
    transfer_timestamps_to_live_text,
)


def coalesce_final_versions(events: list[dict], *, start_s: float, end_s: float) -> list[list[dict]]:
    """Group each transcript row's first/raw and latest/final event versions."""
    grouped: dict[str, list[dict]] = {}
    for item in events:
        if item.get("event") != "transcript_final":
            continue
        data = item.get("data") or {}
        if data.get("speaker_update"):
            continue
        key = str(data.get("id") or f"{data.get('utterance_id')}:{data.get('turn_revision')}")
        grouped.setdefault(key, []).append(data)

    selected = []
    for versions in grouped.values():
        versions.sort(key=lambda row: int(row.get("turn_revision") or 0))
        data = versions[-1]
        start = int(data.get("start_ms") or 0) / 1000.0
        end = int(data.get("end_ms") or 0) / 1000.0
        if end >= start_s and start <= end_s:
            selected.append(versions)
    return sorted(selected, key=lambda rows: (int(rows[0].get("start_ms") or 0), int(rows[0].get("turn_revision") or 0)))


def read_final_versions(path: Path, *, start_s: float, end_s: float) -> list[list[dict]]:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return coalesce_final_versions(events, start_s=start_s, end_s=end_s)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", required=True, help="AliMeeting ID, e.g. R8009_M8018")
    parser.add_argument("--speaker", required=True, help="near-field speaker ID without N_, e.g. SPK8021")
    parser.add_argument("--events", required=True, type=Path, help="live replay JSONL event log")
    parser.add_argument("--data-root", type=Path, default=ROOT / ".local" / "data" / "datasets" / "Eval_Ali")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--time-offset", type=float, default=0.0, help="source recording offset for a clipped replay")
    parser.add_argument("--minimum-turn-duration", type=float, default=0.25)
    parser.add_argument("--device", default=os.environ.get("SENSEVOICE_DEVICE") or os.environ.get("FUNASR_DEVICE", "cuda:0"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.start < 0 or args.duration <= 0:
        raise ValueError("start must be nonnegative and duration must be positive")

    end_s = args.start + args.duration
    source_start_s = args.start + args.time_offset
    source_end_s = end_s + args.time_offset
    tier = f"N_{args.speaker.removeprefix('N_')}"
    audio_path = args.data_root / "Eval_Ali_near" / "audio_dir" / f"{args.recording}_{tier}.wav"
    grid_path = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{args.recording}.TextGrid"
    if not audio_path.is_file() or not grid_path.is_file() or not args.events.is_file():
        raise FileNotFoundError(f"missing near audio, TextGrid, or event log: {audio_path}, {grid_path}, {args.events}")

    info = sf.info(audio_path)
    if info.samplerate != 16000 or info.subtype != "PCM_16":
        raise ValueError(f"expected 16 kHz PCM16 audio, got {info.samplerate} Hz {info.subtype}")
    if info.channels != 1:
        raise ValueError(f"expected one near-field channel, got {info.channels}")

    all_turns = intervals_for_recording(grid_path, end_s=source_end_s)
    all_target_turns = [
        row for row in all_turns
        if row["speaker_tier"] == tier
        and row["start_s"] >= source_start_s
        and row["end_s"] <= source_end_s
        and row["end_s"] - row["start_s"] >= args.minimum_turn_duration
    ]
    clean_turns = clean_reference_turns(
        all_turns,
        start_s=source_start_s,
        end_s=source_end_s,
        minimum_duration_s=args.minimum_turn_duration,
    )
    clean_turns = [row for row in clean_turns if row["speaker_tier"] == tier]
    clean_reference_chars = sum(len(row["reference"]) for row in clean_turns)
    all_reference_chars = sum(len(normalize(str(row["text"]))) for row in all_target_turns)

    versions = read_final_versions(args.events, start_s=args.start, end_s=end_s)
    from funasr import AutoModel

    model_id = os.environ.get("SENSEVOICE_MODEL", str(ROOT / ".local" / "models" / "sensevoice"))
    model = AutoModel(model=model_id, device=args.device, disable_update=True)
    raw_characters: list[dict] = []
    latest_characters: list[dict] = []
    alignments = []
    started = time.perf_counter()
    with sf.SoundFile(audio_path) as audio:
        for rows in versions:
            first, latest = rows[0], rows[-1]
            local_clip_start = max(args.start, int(first.get("start_ms") or 0) / 1000.0)
            local_clip_end = min(end_s, int(latest.get("end_ms") or 0) / 1000.0)
            if local_clip_end - local_clip_start < 0.25:
                continue
            clip_start = local_clip_start + args.time_offset
            clip_end = local_clip_end + args.time_offset
            audio.seek(round(clip_start * info.samplerate))
            samples = audio.read(
                round((clip_end - clip_start) * info.samplerate),
                dtype="int16",
                always_2d=True,
            )[:, 0]
            with tempfile.NamedTemporaryFile(prefix="ali-near-align-", suffix=".wav", delete=False) as handle:
                temp_path = Path(handle.name)
            try:
                sf.write(temp_path, samples, info.samplerate, subtype="PCM_16")
                result = model.generate(
                    input=str(temp_path),
                    batch_size_s=max(1, int(np.ceil(len(samples) / info.samplerate))),
                    output_timestamp=True,
                )
                first_result = result[0] if isinstance(result, list) and result else result
                timestamp_error = ""
                try:
                    acoustic_chars = timestamped_characters(first_result, clip_start_s=clip_start)
                except ValueError as exc:
                    acoustic_chars = []
                    timestamp_error = str(exc)
                raw_text = str(first.get("text") or "")
                latest_text = str(latest.get("text") or "")
                raw_aligned = transfer_timestamps_to_live_text(
                    raw_text, acoustic_chars, clip_start_s=clip_start, clip_end_s=clip_end
                )
                latest_aligned = transfer_timestamps_to_live_text(
                    latest_text, acoustic_chars, clip_start_s=clip_start, clip_end_s=clip_end
                )
                raw_characters.extend(raw_aligned)
                latest_characters.extend(latest_aligned)
                alignments.append(
                    {
                        "start_s": round(clip_start, 3),
                        "end_s": round(clip_end, 3),
                        "event_versions": len(rows),
                        "raw_text": normalize(raw_text),
                        "latest_text": normalize(latest_text),
                        "raw_native_timestamp_chars": sum(not char.get("time_estimated") for char in raw_aligned),
                        "raw_estimated_timestamp_chars": sum(bool(char.get("time_estimated")) for char in raw_aligned),
                        "latest_native_timestamp_chars": sum(not char.get("time_estimated") for char in latest_aligned),
                        "latest_estimated_timestamp_chars": sum(bool(char.get("time_estimated")) for char in latest_aligned),
                        "timestamp_error": timestamp_error,
                    }
                )
            finally:
                temp_path.unlink(missing_ok=True)

    raw_score = score_turns(clean_turns, raw_characters)
    latest_score = score_turns(clean_turns, latest_characters)
    report = {
        "recording": args.recording,
        "speaker_tier": tier,
        "range_s": [args.start, end_s],
        "source_range_s": [source_start_s, source_end_s],
        "score_scope": "complete target-speaker turns with no overlap from any other TextGrid speaker",
        "method": "SenseVoice timestamped re-decode per live-final span; edit-transfer timestamps to first and latest stored final text",
        "source_final_count": len(versions),
        "all_target_reference_turns": len(all_target_turns),
        "clean_target_reference_turns": len(clean_turns),
        "all_target_reference_chars": all_reference_chars,
        "clean_target_reference_chars": clean_reference_chars,
        "clean_reference_coverage": round(clean_reference_chars / all_reference_chars, 4) if all_reference_chars else None,
        "runtime_s": round(time.perf_counter() - started, 3),
        "raw_first_final": raw_score,
        "latest_final": latest_score,
        "alignment_runs": alignments,
        "limitations": [
            "SenseVoice timestamps are predicted acoustic times, not forced alignment.",
            "Replacements and insertions receive estimated timestamps; counts are reported per final span.",
            "Overlap and short target turns are excluded, so this diagnostic does not replace full near-field CER.",
        ],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(rendered + "\n", encoding="utf-8")
    print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
