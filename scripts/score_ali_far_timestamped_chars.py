#!/usr/bin/env python3
"""Score non-overlapping AliMeeting turns using SenseVoice character times.

This diagnostic re-runs the local final recognizer on each live final's audio
span with SenseVoice timestamp output enabled. Recognized characters are then
assigned to clean TextGrid turns by their aligned audio times. It is useful for
measuring natural far-field audio where live final spans merge multiple turns;
it is not a strict score of the stored live-final text because the aligned
hypothesis is freshly decoded by SenseVoice.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import jiwer
import numpy as np
import soundfile as sf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from scripts.diagnose_ali_final_alignment import intervals_for_recording, read_final_events
from scripts.evaluate_ali_realtime import normalize


def clean_reference_turns(rows: list[dict], *, start_s: float, end_s: float, minimum_duration_s: float) -> list[dict]:
    selected = []
    for row in rows:
        start, end = float(row["start_s"]), float(row["end_s"])
        if start < start_s or end > end_s or end - start < minimum_duration_s:
            continue
        overlaps = any(
            other["speaker_tier"] != row["speaker_tier"]
            and max(start, float(other["start_s"])) < min(end, float(other["end_s"]))
            for other in rows
        )
        if not overlaps:
            selected.append({**row, "reference": normalize(str(row["text"]))})
    return sorted(selected, key=lambda row: (row["start_s"], row["speaker_tier"]))


def timestamped_characters(result: dict, *, clip_start_s: float) -> list[dict]:
    """Return (char, global start/end) entries or fail closed on mismatches."""
    timestamps = result.get("timestamp") or result.get("timestamps") or []
    words = result.get("words")
    if not isinstance(words, list) or len(words) != len(timestamps):
        text = normalize(str(result.get("text") or ""))
        if len(text) != len(timestamps):
            raise ValueError(
                f"cannot map model text to timestamps: chars={len(text)} timestamps={len(timestamps)}"
            )
        words = list(text)

    output = []
    for token, stamp in zip(words, timestamps):
        if not isinstance(stamp, (list, tuple)) or len(stamp) < 2:
            continue
        start_ms, end_ms = float(stamp[0]), float(stamp[1])
        if end_ms < start_ms:
            continue
        chars = normalize(str(token))
        for char in chars:
            output.append(
                {
                    "char": char,
                    "start_s": clip_start_s + start_ms / 1000.0,
                    "end_s": clip_start_s + end_ms / 1000.0,
                }
            )
    return output


def transfer_timestamps_to_live_text(
    live_text: str,
    aligned_characters: list[dict],
    *,
    clip_start_s: float,
    clip_end_s: float,
) -> list[dict]:
    """Transfer acoustic times from a same-span ASR decode to stored final text.

    Exact character matches inherit their native time. Replacement and inserted
    characters receive a monotonic interpolated time and are marked estimated.
    Callers must report that distinction because these are not forced-alignment
    timestamps for the stored text.
    """
    target = normalize(live_text)
    source = [str(item["char"]) for item in aligned_characters]
    output: list[dict | None] = [None] * len(target)
    matcher = difflib.SequenceMatcher(a=source, b=list(target), autojunk=False)
    for tag, source_start, source_end, target_start, target_end in matcher.get_opcodes():
        target_count = target_end - target_start
        source_count = source_end - source_start
        if tag == "equal":
            for offset in range(target_count):
                original = aligned_characters[source_start + offset]
                output[target_start + offset] = {**original, "char": target[target_start + offset], "time_estimated": False}
            continue
        if tag == "replace" and source_count:
            for offset in range(target_count):
                source_offset = min(source_count - 1, int(offset * source_count / max(1, target_count)))
                original = aligned_characters[source_start + source_offset]
                output[target_start + offset] = {**original, "char": target[target_start + offset], "time_estimated": True}
            continue
        if tag != "insert":
            continue

        previous = aligned_characters[source_start - 1] if source_start > 0 else None
        following = aligned_characters[source_start] if source_start < len(aligned_characters) else None
        left = float(previous["end_s"]) if previous else clip_start_s
        right = float(following["start_s"]) if following else clip_end_s
        if right < left:
            left, right = min(left, right), max(left, right)
        if right == left:
            right = min(clip_end_s, left + 0.06)
        for offset in range(target_count):
            start = left + (right - left) * offset / max(1, target_count)
            end = left + (right - left) * (offset + 1) / max(1, target_count)
            output[target_start + offset] = {
                "char": target[target_start + offset],
                "start_s": start,
                "end_s": max(start, end),
                "time_estimated": True,
            }

    # If the recognizer emitted no characters at all, spread the live text over
    # its source final span instead of dropping it from the accuracy denominator.
    if target and all(item is None for item in output):
        span = max(0.0, clip_end_s - clip_start_s)
        for index, char in enumerate(target):
            start = clip_start_s + span * index / len(target)
            end = clip_start_s + span * (index + 1) / len(target)
            output[index] = {"char": char, "start_s": start, "end_s": end, "time_estimated": True}
    return [item for item in output if item is not None]


def score_turns(turns: list[dict], characters: list[dict]) -> dict:
    scored = []
    for turn in turns:
        start, end = float(turn["start_s"]), float(turn["end_s"])
        hypothesis = "".join(
            item["char"]
            for item in characters
            if item["start_s"] >= start and item["end_s"] <= end
        )
        reference = str(turn["reference"])
        scored.append(
            {
                "speaker_tier": turn["speaker_tier"],
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "reference": reference,
                "hypothesis": hypothesis,
                "reference_chars": len(reference),
                "hypothesis_chars": len(hypothesis),
                "cer": round(float(jiwer.cer(reference, hypothesis)), 4),
            }
        )
    reference = "".join(row["reference"] for row in scored)
    hypothesis = "".join(row["hypothesis"] for row in scored)
    return {
        "scored_clean_turn_count": len(scored),
        "reference_chars": len(reference),
        "hypothesis_chars": len(hypothesis),
        "character_weighted_cer": round(float(jiwer.cer(reference, hypothesis)), 4) if reference else None,
        "turns": scored,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT / ".local" / "data" / "datasets" / "Eval_Ali")
    parser.add_argument("--recording", required=True, help="meeting ID without _MSxxx, e.g. R8003_M8001")
    parser.add_argument("--events", type=Path, required=True, help="live replay event JSONL")
    parser.add_argument("--channel", type=int, default=3)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--time-offset", type=float, default=0.0)
    parser.add_argument("--minimum-turn-duration", type=float, default=0.6)
    parser.add_argument("--device", default=os.environ.get("FUNASR_DEVICE", "cuda:0"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.start < 0 or args.duration <= 0:
        raise ValueError("start must be nonnegative and duration must be positive")

    audio_dir = args.data_root / "Eval_Ali_far" / "audio_dir"
    audio_path = audio_dir / f"{args.recording}_MS801.wav"
    grid_path = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{args.recording}.TextGrid"
    if not audio_path.is_file() or not grid_path.is_file():
        raise FileNotFoundError(f"AliMeeting audio or TextGrid missing for {args.recording}")
    info = sf.info(audio_path)
    if info.samplerate != 16000 or info.subtype != "PCM_16":
        raise ValueError(f"expected 16 kHz PCM16 audio, got {info.samplerate} Hz {info.subtype}")
    if not 0 <= args.channel < info.channels:
        raise ValueError(f"channel must be between 0 and {info.channels - 1}")
    window_start = args.start + args.time_offset
    window_end = window_start + args.duration
    if window_end > info.duration:
        raise ValueError(f"requested clip exceeds recording duration {info.duration:.3f}s")

    finals = read_final_events(
        args.events,
        start_s=args.start,
        end_s=args.start + args.duration,
        time_offset_s=args.time_offset,
    )
    from funasr import AutoModel

    model_id = os.environ.get("SENSEVOICE_MODEL", str(ROOT / ".local" / "models" / "sensevoice"))
    model = AutoModel(model=model_id, device=args.device, disable_update=True)
    turns = clean_reference_turns(
        intervals_for_recording(grid_path, end_s=window_end),
        start_s=window_start,
        end_s=window_end,
        minimum_duration_s=args.minimum_turn_duration,
    )

    aligned_chars = []
    live_chars_for_scoring = []
    replayed = []
    started = time.perf_counter()
    with sf.SoundFile(audio_path) as audio:
        for event in finals:
            data = event.get("data") or {}
            start_s = int(data.get("start_ms") or 0) / 1000.0
            end_s = int(data.get("end_ms") or 0) / 1000.0
            if end_s - start_s < 0.25:
                continue
            clip_start = max(start_s, window_start)
            clip_end = min(end_s, window_end)
            if clip_end <= clip_start:
                continue
            audio.seek(round(clip_start * info.samplerate))
            samples = audio.read(round((clip_end - clip_start) * info.samplerate), dtype="int16", always_2d=True)
            samples = samples[:, args.channel]
            with tempfile.NamedTemporaryFile(prefix="ali-align-", suffix=".wav", delete=False) as handle:
                temp_path = Path(handle.name)
            try:
                sf.write(temp_path, samples, info.samplerate, subtype="PCM_16")
                result = model.generate(input=str(temp_path), batch_size_s=max(1, int(np.ceil(len(samples) / info.samplerate))), output_timestamp=True)
                first = result[0] if isinstance(result, list) and result else result
                if not isinstance(first, dict):
                    continue
                chars = timestamped_characters(first, clip_start_s=clip_start)
                aligned_chars.extend(chars)
                model_text = "".join(item["char"] for item in chars)
                live_text = normalize(str(data.get("text") or ""))
                live_alignment = transfer_timestamps_to_live_text(
                    live_text,
                    chars,
                    clip_start_s=clip_start,
                    clip_end_s=clip_end,
                )
                live_chars_for_scoring.extend(live_alignment)
                replayed.append(
                    {
                        "segment_id": data.get("id"),
                        "start_s": round(clip_start, 3),
                        "end_s": round(clip_end, 3),
                        "aligned_character_count": len(chars),
                        "model_text": model_text,
                        "stored_live_final_text": live_text,
                        "live_final_vs_timestamped_decode_cer": round(float(jiwer.cer(live_text, model_text)), 4)
                        if live_text and model_text
                        else None,
                        "live_final_character_count": len(live_alignment),
                        "native_timestamps_used": sum(not item.get("time_estimated") for item in live_alignment),
                        "estimated_timestamps_used": sum(bool(item.get("time_estimated")) for item in live_alignment),
                    }
                )
            finally:
                temp_path.unlink(missing_ok=True)

    report = {
        "method": "SenseVoice re-decodes each live-final audio span; edit-aligned stored live-final characters inherit native or interpolated character times, then complete non-overlapping TextGrid turns are scored",
        "score_type": "stored live-final text CER with approximate timestamp transfer from a same-span SenseVoice decode",
        "recording": args.recording,
        "channel": args.channel,
        "window_start_s": window_start,
        "window_end_s": window_end,
        "source_final_count": len(finals),
        "aligned_final_count": len(replayed),
        "runtime_s": round(time.perf_counter() - started, 3),
        "alignment_runs": replayed,
        **score_turns(turns, live_chars_for_scoring),
        "limitations": [
            "A fresh SenseVoice decode supplies times; the scored characters are the stored live-final text.",
            "Matching characters inherit model timestamps, while replacements and insertions receive approximate interpolated times; inspect per-span counts.",
            "Characters must fall fully inside a complete non-overlapping TextGrid speaker turn; overlap and boundary-crossing turns are excluded.",
            "SenseVoice timestamps are model-predicted character times, not manually verified word alignments.",
        ],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
        print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
