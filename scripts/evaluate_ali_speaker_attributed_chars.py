#!/usr/bin/env python3
"""Evaluate timestamp transfer plus CAM++ window labels on live final text.

This diagnostic combines a stored live-final event, SenseVoice native character
times (with edit-based timestamp transfer called out separately), CAM++ sliding
window labels, and AliMeeting TextGrid speaker turns. It does not write changes
to meetings or transcript rows.
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

from scripts.diagnose_ali_final_alignment import intervals_for_recording, read_final_events
from scripts.evaluate_ali_far_nonoverlap import resolve_far_audio
from scripts.score_ali_far_timestamped_chars import (
    timestamped_characters,
    transfer_timestamps_to_live_text,
)


def label_for_span(start_s: float, end_s: float, windows: list[dict], *, minimum_coverage: float = 0.6,
                   minimum_vote_share: float = 0.65, assignment_mode: str = "overlap") -> tuple[str | None, float, float]:
    """Vote over predicted CAM++ windows; abstain on low coverage or conflict."""
    duration = max(0.0, end_s - start_s)
    if duration <= 0:
        return None, 0.0, 0.0
    if assignment_mode == "center":
        centers = sorted({(float(row["start_s"]) + float(row["end_s"])) / 2.0 for row in windows})
        if not centers:
            return None, 0.0, 0.0
        midpoint = (start_s + end_s) / 2.0
        center = min(centers, key=lambda item: abs(item - midpoint))
        steps = [right - left for left, right in zip(centers, centers[1:]) if right > left]
        radius = min(steps) / 2.0 + 1e-6 if steps else 0.75
        if abs(center - midpoint) > radius:
            return None, 0.0, 0.0
        nearest = next(row for row in windows if (float(row["start_s"]) + float(row["end_s"])) / 2.0 == center)
        label = nearest.get("predicted")
        return (str(label), 1.0, 1.0) if label is not None else (None, 0.0, 0.0)
    if assignment_mode != "overlap":
        raise ValueError(f"unsupported assignment mode: {assignment_mode}")
    votes: dict[str, float] = {}
    covered: list[tuple[float, float]] = []
    for row in windows:
        speaker = row.get("predicted")
        if speaker is None:
            continue
        left = max(start_s, float(row["start_s"]))
        right = min(end_s, float(row["end_s"]))
        if right <= left:
            continue
        votes[str(speaker)] = votes.get(str(speaker), 0.0) + right - left
        covered.append((left, right))
    if not votes:
        return None, 0.0, 0.0
    covered.sort()
    union = 0.0
    left, right = covered[0]
    for next_left, next_right in covered[1:]:
        if next_left <= right:
            right = max(right, next_right)
        else:
            union += right - left
            left, right = next_left, next_right
    union += right - left
    coverage = min(1.0, union / duration)
    total_votes = sum(votes.values())
    ordered = sorted(votes.items(), key=lambda item: item[1], reverse=True)
    share = ordered[0][1] / total_votes
    label = ordered[0][0] if coverage >= minimum_coverage and share >= minimum_vote_share else None
    return label, coverage, share


def reference_label_at(midpoint_s: float, turns: list[dict]) -> str | None:
    active = {
        str(row["speaker_tier"])
        for row in turns
        if float(row["start_s"]) <= midpoint_s < float(row["end_s"])
    }
    return next(iter(active)) if len(active) == 1 else None


def score_characters(characters: list[dict], turns: list[dict], windows: list[dict], *,
                     minimum_coverage: float, minimum_vote_share: float,
                     assignment_mode: str = "overlap") -> dict:
    scored = []
    for char in characters:
        start, end = float(char["start_s"]), float(char["end_s"])
        midpoint = (start + end) / 2.0
        reference = reference_label_at(midpoint, turns)
        predicted, coverage, vote_share = label_for_span(
            start, end, windows,
            minimum_coverage=minimum_coverage,
            minimum_vote_share=minimum_vote_share,
            assignment_mode=assignment_mode,
        )
        scored.append({
            "char": str(char["char"]), "start_s": round(start, 4), "end_s": round(end, 4),
            "time_estimated": bool(char.get("time_estimated")),
            "reference_speaker": reference, "predicted_speaker": predicted,
            "window_coverage": round(coverage, 4), "vote_share": round(vote_share, 4),
        })

    def metrics(rows: list[dict]) -> dict:
        eligible = [row for row in rows if row["reference_speaker"] is not None]
        labeled = [row for row in eligible if row["predicted_speaker"] is not None]
        correct = sum(row["reference_speaker"] == row["predicted_speaker"] for row in labeled)
        return {
            "reference_scored_chars": len(eligible), "speaker_labeled_chars": len(labeled),
            "label_coverage": round(len(labeled) / len(eligible), 4) if eligible else None,
            "correct_chars": correct,
            "accuracy_when_labeled": round(correct / len(labeled), 4) if labeled else None,
        }

    native = [row for row in scored if not row["time_estimated"]]
    estimated = [row for row in scored if row["time_estimated"]]
    return {
        "all_timestamp_transfer": metrics(scored),
        "native_timestamps_only": metrics(native),
        "estimated_timestamps_only": metrics(estimated),
        "unscorable_overlap_or_silence_chars": sum(row["reference_speaker"] is None for row in scored),
        "characters": scored,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", required=True, help="AliMeeting ID, e.g. R8001_M8004")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--window-report", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / ".local" / "data" / "datasets" / "Eval_Ali")
    parser.add_argument("--channel", type=int, default=3)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--time-offset", type=float, default=0.0)
    parser.add_argument("--device", default=os.environ.get("SENSEVOICE_DEVICE") or os.environ.get("FUNASR_DEVICE", "cuda:0"))
    parser.add_argument("--minimum-window-coverage", type=float, default=0.6)
    parser.add_argument("--minimum-vote-share", type=float, default=0.65)
    parser.add_argument("--assignment-mode", choices=("overlap", "center"), default="overlap")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    audio_path, _ = resolve_far_audio(args.data_root / "Eval_Ali_far" / "audio_dir", args.recording)
    info = sf.info(audio_path)
    if info.samplerate != 16000 or not 0 <= args.channel < info.channels:
        raise ValueError(f"expected 16 kHz audio and valid channel, got {info.samplerate} Hz / {info.channels} channels")
    window_start = args.start + args.time_offset
    window_end = window_start + args.duration
    if window_start < 0 or window_end > info.duration:
        raise ValueError(f"requested range exceeds recording duration {info.duration:.3f}s")

    window_report = json.loads(args.window_report.read_text(encoding="utf-8"))
    if window_report.get("recording") != args.recording or window_report.get("channel") != args.channel:
        raise ValueError("window report recording/channel does not match the requested audio")
    windows = [
        row for row in window_report.get("windows", [])
        if float(row["start_s"]) >= window_start and float(row["end_s"]) <= window_end
    ]
    if not windows:
        raise ValueError("window report contains no complete windows in the requested interval")

    events = read_final_events(
        args.events, start_s=args.start, end_s=args.start + args.duration,
        time_offset_s=args.time_offset,
    )
    turns = intervals_for_recording(
        args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{args.recording}.TextGrid",
        end_s=window_end,
    )
    from funasr import AutoModel

    model_id = os.environ.get("SENSEVOICE_MODEL", str(ROOT / ".local" / "models" / "sensevoice"))
    model = AutoModel(model=model_id, device=args.device, disable_update=True)
    scored_chars = []
    alignment_runs = []
    started = time.perf_counter()
    with sf.SoundFile(audio_path) as audio:
        for event in events:
            data = event.get("data") or {}
            start_s = max(window_start, int(data.get("start_ms") or 0) / 1000.0)
            end_s = min(window_end, int(data.get("end_ms") or 0) / 1000.0)
            live_text = str(data.get("text") or "")
            if end_s <= start_s or not live_text.strip():
                continue
            audio.seek(round(start_s * info.samplerate))
            samples = audio.read(round((end_s - start_s) * info.samplerate), dtype="int16", always_2d=True)[:, args.channel]
            with tempfile.NamedTemporaryFile(prefix="ali-speaker-align-", suffix=".wav", delete=False) as handle:
                temp_path = Path(handle.name)
            try:
                sf.write(temp_path, samples, info.samplerate, subtype="PCM_16")
                result = model.generate(
                    input=str(temp_path), batch_size_s=max(1, int(np.ceil(len(samples) / info.samplerate))),
                    output_timestamp=True,
                )
                first = result[0] if isinstance(result, list) and result else result
                if not isinstance(first, dict):
                    continue
                native_chars = timestamped_characters(first, clip_start_s=start_s)
                aligned_live = transfer_timestamps_to_live_text(
                    live_text, native_chars, clip_start_s=start_s, clip_end_s=end_s
                )
                scored_chars.extend(aligned_live)
                alignment_runs.append({
                    "segment_id": data.get("id"), "start_s": round(start_s, 3), "end_s": round(end_s, 3),
                    "live_text_chars": len(aligned_live), "native_timestamp_chars": sum(not c.get("time_estimated") for c in aligned_live),
                    "estimated_timestamp_chars": sum(bool(c.get("time_estimated")) for c in aligned_live),
                    "sensevoice_text": "".join(c["char"] for c in native_chars),
                    "live_vs_sensevoice_char_error_rate": None,
                })
                if native_chars:
                    import jiwer
                    from scripts.evaluate_ali_realtime import normalize
                    alignment_runs[-1]["live_vs_sensevoice_char_error_rate"] = round(
                        float(jiwer.cer(normalize(live_text), "".join(c["char"] for c in native_chars))), 4
                    )
            finally:
                temp_path.unlink(missing_ok=True)

    scores = score_characters(
        scored_chars, turns, windows,
        minimum_coverage=args.minimum_window_coverage,
        minimum_vote_share=args.minimum_vote_share,
        assignment_mode=args.assignment_mode,
    )
    report = {
        "method": "SenseVoice character timestamps transferred to stored live-final text, then voted against overlapping CAM++ sliding-window labels; reference speakers read from TextGrid",
        "recording": args.recording, "channel": args.channel,
        "start_s": window_start, "end_s": window_end,
        "events": str(args.events), "window_report": str(args.window_report),
        "source_final_count": len(events), "aligned_final_count": len(alignment_runs),
        "runtime_s": round(time.perf_counter() - started, 3),
        "minimum_window_coverage": args.minimum_window_coverage,
        "minimum_vote_share": args.minimum_vote_share,
        "assignment_mode": args.assignment_mode,
        "alignment_runs": alignment_runs,
        **scores,
        "limitations": [
            "Offline diagnostic only; no transcript records or production speaker labels are changed.",
            "SenseVoice provides times for a fresh same-span decode; edit-matched live characters inherit native times, while replacements/insertions have estimated times.",
            "Speaker correctness is measured only where a character midpoint lies inside exactly one TextGrid speaker turn; overlap/silence is unscorable.",
            "CAM++ windows use supervised speaker prototypes from the companion report; results do not prove unsupervised online diarization.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("characters", "alignment_runs")}, ensure_ascii=False, indent=2))
    print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
