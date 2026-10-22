#!/usr/bin/env python3
"""Score live speaker labels against AliMeeting far-field TextGrid intervals.

This computes overlap-aware, permutation-invariant DER from the timestamps and
speaker labels attached to final transcript rows. It is a diagnostic for the
current turn-level clustering path; broad utterance spans and included silence
are scored as emitted because they are part of the user-visible result.
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.diagnose_ali_final_alignment import intervals_for_recording, read_final_events


def _clip(start: float, end: float, window_start: float, window_end: float) -> tuple[float, float] | None:
    left, right = max(start, window_start), min(end, window_end)
    return (left, right) if right > left else None


def score_der(
    references: list[dict],
    hypotheses: list[dict],
    *,
    start_s: float,
    end_s: float,
) -> dict:
    """Return overlap-aware DER with an optimal global speaker mapping.

    The score uses zero collar and no forgiveness collar. Time with no reference
    speaker is retained so emitted speech during annotated silence counts as a
    false alarm. Duplicate same-speaker spans count once per instant.
    """
    refs: list[tuple[float, float, str]] = []
    hyps: list[tuple[float, float, str]] = []
    for row in references:
        clipped = _clip(float(row["start_s"]), float(row["end_s"]), start_s, end_s)
        if clipped:
            refs.append((*clipped, str(row["speaker_tier"])))
    for row in hypotheses:
        clipped = _clip(float(row["start_s"]), float(row["end_s"]), start_s, end_s)
        if clipped:
            hyps.append((*clipped, str(row["speaker"] or "unknown")))

    ref_labels = sorted({speaker for _, _, speaker in refs})
    hyp_labels = sorted({speaker for _, _, speaker in hyps})
    boundaries = sorted(
        {start_s, end_s}
        | {t for left, right, _ in refs + hyps for t in (left, right)}
    )
    affinity = {hyp: {ref: 0.0 for ref in ref_labels} for hyp in hyp_labels}
    spans: list[tuple[float, float, set[str], set[str]]] = []
    for left, right in zip(boundaries, boundaries[1:]):
        if right <= left:
            continue
        middle = (left + right) / 2.0
        active_ref = {speaker for a, b, speaker in refs if a <= middle < b}
        active_hyp = {speaker for a, b, speaker in hyps if a <= middle < b}
        duration = right - left
        for hyp in active_hyp:
            for ref in active_ref:
                affinity[hyp][ref] += duration
        spans.append((duration, left, active_ref, active_hyp))

    # Exact maximum-weight one-to-one mapping; AliMeeting has only a handful
    # of speakers, so bitmask DP avoids requiring SciPy in the base install.
    @lru_cache(maxsize=None)
    def best(index: int, used_refs: int) -> tuple[float, tuple[int, ...]]:
        if index >= len(hyp_labels):
            return 0.0, ()
        hyp = hyp_labels[index]
        score, tail = best(index + 1, used_refs)
        choice = (-1, *tail)
        for ref_index, ref in enumerate(ref_labels):
            bit = 1 << ref_index
            if used_refs & bit:
                continue
            rest_score, rest_map = best(index + 1, used_refs | bit)
            candidate = affinity[hyp][ref] + rest_score
            if candidate > score:
                score, choice = candidate, (ref_index, *rest_map)
        return score, choice

    _matched_duration, assignment = best(0, 0)
    mapping = {
        hyp_labels[index]: ref_labels[ref_index]
        for index, ref_index in enumerate(assignment)
        if ref_index >= 0
    }

    missed = false_alarm = confusion = reference_speaker_seconds = 0.0
    for duration, _left, active_ref, active_hyp in spans:
        mapped_hyp = {mapping[label] for label in active_hyp if label in mapping}
        n_ref, n_hyp = len(active_ref), len(active_hyp)
        correct = len(active_ref & mapped_hyp)
        missed += max(0, n_ref - n_hyp) * duration
        false_alarm += max(0, n_hyp - n_ref) * duration
        confusion += max(0, min(n_ref, n_hyp) - correct) * duration
        reference_speaker_seconds += n_ref * duration

    total_error = missed + false_alarm + confusion
    return {
        "method": "overlap-aware DER, zero collar, optimal global speaker mapping, emitted spans including silence",
        "start_s": start_s,
        "end_s": end_s,
        "reference_speaker_count": len(ref_labels),
        "hypothesis_speaker_count": len(hyp_labels),
        "reference_speaker_seconds": round(reference_speaker_seconds, 3),
        "missed_speaker_seconds": round(missed, 3),
        "false_alarm_speaker_seconds": round(false_alarm, 3),
        "confusion_speaker_seconds": round(confusion, 3),
        "der": round(total_error / reference_speaker_seconds, 4) if reference_speaker_seconds else None,
        "speaker_mapping": mapping,
        "reference_turn_count": len(refs),
        "hypothesis_turn_count": len(hyps),
        "limitations": [
            "This evaluates speaker labels and utterance-level time spans, not word-level attribution.",
            "Long final spans and their included pauses are scored as emitted; no collar is applied.",
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
    if args.start < 0 or args.duration <= 0:
        raise ValueError("start must be nonnegative and duration must be positive")
    end_s = args.start + args.duration
    grid = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{args.recording}.TextGrid"
    if not grid.is_file():
        raise FileNotFoundError(grid)
    refs = intervals_for_recording(grid, end_s=end_s)
    finals = read_final_events(
        args.events,
        start_s=args.start,
        end_s=end_s,
        time_offset_s=args.time_offset,
    )
    hyps = [
        {
            "start_s": int((item.get("data") or {}).get("start_ms") or 0) / 1000.0,
            "end_s": int((item.get("data") or {}).get("end_ms") or 0) / 1000.0,
            "speaker": (item.get("data") or {}).get("speaker"),
        }
        for item in finals
    ]
    report = score_der(refs, hyps, start_s=args.start, end_s=end_s)
    report.update({"recording": args.recording, "events": str(args.events)})
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
