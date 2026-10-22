#!/usr/bin/env python3
"""Check whether natural far-field final rows can be scored unambiguously.

Final transcript rows contain only utterance-level offsets, while AliMeeting
TextGrid references are speaker-tier utterances. This report identifies final
rows that contain complete, non-overlapping reference utterances and refuses
to assign CER to rows crossing overlap or annotation boundaries.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jiwer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.evaluate_ali_far_nonoverlap import read_tiers
from scripts.evaluate_ali_realtime import normalize


def read_final_events(
    path: Path,
    *,
    start_s: float,
    end_s: float,
    time_offset_s: float = 0.0,
) -> list[dict]:
    latest: dict[str, dict] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("event") != "transcript_final":
                continue
            data = item.get("data") or {}
            key = str(data.get("id") or f"{data.get('utterance_id')}:{data.get('turn_revision')}")
            previous = latest.get(key)
            if previous is None or float(item.get("wall_s") or 0) >= float(previous.get("wall_s") or 0):
                shifted = dict(item)
                shifted_data = dict(data)
                shift_ms = round(time_offset_s * 1000)
                shifted_data["start_ms"] = int(shifted_data.get("start_ms") or 0) + shift_ms
                shifted_data["end_ms"] = int(shifted_data.get("end_ms") or 0) + shift_ms
                shifted["data"] = shifted_data
                latest[key] = {**shifted, "_line": line_number}

    selected = []
    for item in latest.values():
        data = item.get("data") or {}
        start = int(data.get("start_ms") or 0) / 1000.0
        end = int(data.get("end_ms") or 0) / 1000.0
        if end < start or end < start_s or start > end_s:
            continue
        selected.append(item)
    return sorted(selected, key=lambda row: (int((row.get("data") or {}).get("start_ms") or 0), float(row.get("wall_s") or 0)))


def intervals_for_recording(grid_path: Path, *, end_s: float) -> list[dict]:
    tiers = read_tiers(grid_path)
    rows = []
    for speaker, intervals in tiers.items():
        if not speaker.startswith("N_SPK"):
            continue
        for start, end, text in intervals:
            if start < end_s and end > 0:
                rows.append({"speaker_tier": speaker, "start_s": start, "end_s": end, "text": normalize(text)})
    return sorted(rows, key=lambda row: (row["start_s"], row["speaker_tier"]))


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def intersection_duration(start: float, end: float, other_start: float, other_end: float) -> float:
    return max(0.0, min(end, other_end) - max(start, other_start))


def analyze(events: list[dict], rows: list[dict], *, start_s: float, end_s: float) -> dict:
    pairwise_overlaps = []
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if left["speaker_tier"] == right["speaker_tier"]:
                continue
            start = max(left["start_s"], right["start_s"])
            end = min(left["end_s"], right["end_s"])
            if start < end:
                pairwise_overlaps.append((start, end))
    overlap_regions = merge_intervals(pairwise_overlaps)

    scored: list[tuple[str, str]] = []
    final_rows = []
    for item in events:
        data = item.get("data") or {}
        start = int(data.get("start_ms") or 0) / 1000.0
        end = int(data.get("end_ms") or 0) / 1000.0
        contained = [row for row in rows if row["start_s"] >= start and row["end_s"] <= end]
        crossing = [
            row
            for row in rows
            if intersection_duration(start, end, row["start_s"], row["end_s"]) > 0
            and row not in contained
        ]
        overlapping_refs = [
            row
            for row in contained
            if any(
                other["speaker_tier"] != row["speaker_tier"]
                and intersection_duration(row["start_s"], row["end_s"], other["start_s"], other["end_s"]) > 0
                for other in rows
            )
        ]
        overlap_s = sum(intersection_duration(start, end, left, right) for left, right in overlap_regions)
        clean_refs = sorted(
            [row for row in contained if row not in overlapping_refs],
            key=lambda row: (row["start_s"], row["speaker_tier"]),
        )
        reference = "".join(row["text"] for row in clean_refs)
        hypothesis = normalize(str(data.get("text") or ""))
        eligible = bool(reference) and not crossing and not overlapping_refs and overlap_s == 0 and end > start
        cer = float(jiwer.cer(reference, hypothesis)) if eligible else None
        if eligible:
            scored.append((reference, hypothesis))
        final_rows.append(
            {
                "segment_id": data.get("id"),
                "utterance_id": data.get("utterance_id"),
                "turn_revision": data.get("turn_revision"),
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "duration_s": round(max(0.0, end - start), 3),
                "speaker_tiers_inside": sorted({row["speaker_tier"] for row in contained}),
                "complete_reference_turns": len(contained),
                "boundary_crossing_reference_turns": len(crossing),
                "overlapping_reference_turns": len(overlapping_refs),
                "cross_speaker_overlap_s": round(overlap_s, 3),
                "clean_reference_chars": len(reference),
                "hypothesis_chars": len(hypothesis),
                "strict_score_eligible": eligible,
                "cer": round(cer, 4) if cer is not None else None,
            }
        )

    reference = "".join(ref for ref, _ in scored)
    hypothesis = "".join(hyp for _, hyp in scored)
    return {
        "method": "final-span CER only when complete TextGrid utterances are contained and no cross-speaker overlap or boundary crossing touches the final span",
        "start_s": start_s,
        "end_s": end_s,
        "textgrid_speaker_turn_count": len(rows),
        "cross_speaker_overlap_region_count": len(overlap_regions),
        "cross_speaker_overlap_duration_s": round(sum(end - start for start, end in overlap_regions), 3),
        "unique_final_segment_count": len(events),
        "strictly_scoreable_final_count": len(scored),
        "strict_reference_chars": len(reference),
        "strict_final_cer": round(float(jiwer.cer(reference, hypothesis)), 4) if reference else None,
        "final_rows": final_rows,
        "limitations": [
            "A final row crossing a speaker overlap or TextGrid interval boundary is not assigned a CER.",
            "This checks whether existing utterance-level offsets support scoring; it does not perform word-level forced alignment.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT / ".local" / "data" / "datasets" / "Eval_Ali")
    parser.add_argument("--recording", required=True, help="meeting ID without _MSxxx, e.g. R8001_M8004")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument(
        "--time-offset",
        type=float,
        default=0.0,
        help="seconds to add to replay event timestamps when the audio clip starts later in the source recording",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    grid = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{args.recording}.TextGrid"
    if not grid.is_file():
        raise FileNotFoundError(grid)
    if args.start < 0 or args.duration <= 0:
        raise ValueError("start must be nonnegative and duration must be positive")
    end_s = args.start + args.duration
    rows = [row for row in intervals_for_recording(grid, end_s=end_s) if row["end_s"] > args.start]
    events = read_final_events(
        args.events,
        start_s=args.start,
        end_s=end_s,
        time_offset_s=args.time_offset,
    )
    report = analyze(events, rows, start_s=args.start, end_s=end_s)
    report.update({"recording": args.recording, "events": str(args.events)})
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "final_rows"}, ensure_ascii=False, indent=2))
    if args.report:
        print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
