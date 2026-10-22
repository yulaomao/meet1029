#!/usr/bin/env python3
"""Evaluate CAM++ sliding-window speaker labels against AliMeeting turns.

The speaker prototypes are built only from clean reference turns outside the
requested evaluation interval. Evaluation windows are classified by cosine
similarity and scored only when a single reference speaker covers enough of
the window without cross-speaker overlap. This is an offline feasibility test,
not a production diarization implementation.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import math
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.providers.embedding import CampPlusSpeakerEmbeddingProvider
from scripts.diagnose_ali_final_alignment import intervals_for_recording
from scripts.evaluate_ali_far_nonoverlap import resolve_far_audio


def cosine(left: list[float], right: list[float]) -> float:
    a, b = np.asarray(left, dtype=np.float64), np.asarray(right, dtype=np.float64)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else -1.0


def clean_reference_label(
    rows: list[dict], start_s: float, end_s: float, *, minimum_coverage: float = 0.70
) -> tuple[str | None, float, bool]:
    """Return dominant reference label, coverage, and overlap ambiguity."""
    durations: dict[str, float] = {}
    any_overlap = False
    for row in rows:
        overlap = max(0.0, min(end_s, float(row["end_s"])) - max(start_s, float(row["start_s"])))
        if overlap <= 0:
            continue
        speaker = str(row["speaker_tier"])
        durations[speaker] = durations.get(speaker, 0.0) + overlap
        for other in rows:
            if other["speaker_tier"] == speaker:
                continue
            if max(start_s, float(row["start_s"]), float(other["start_s"])) < min(
                end_s, float(row["end_s"]), float(other["end_s"])
            ):
                any_overlap = True
    if not durations:
        return None, 0.0, any_overlap
    speaker, duration = max(durations.items(), key=lambda item: item[1])
    coverage = duration / max(1e-9, end_s - start_s)
    return (speaker if coverage >= minimum_coverage and not any_overlap else None), coverage, any_overlap


def classify_similarities(scores: dict[str, float], *, minimum_score: float, minimum_margin: float) -> tuple[str | None, float, float]:
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return None, -1.0, 0.0
    margin = ordered[0][1] - (ordered[1][1] if len(ordered) > 1 else -1.0)
    label = ordered[0][0] if ordered[0][1] >= minimum_score and margin >= minimum_margin else None
    return label, ordered[0][1], margin


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil((len(ordered) - 1) * p))]


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    output = io.BytesIO()
    sf.write(output, samples, sample_rate, format="WAV", subtype="PCM_16")
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT / ".local" / "data" / "datasets" / "Eval_Ali")
    parser.add_argument("--recording", required=True, help="meeting ID, e.g. R8003_M8001")
    parser.add_argument("--channel", type=int, default=3)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--window", type=float, default=3.0)
    parser.add_argument("--hop", type=float, default=1.5)
    parser.add_argument("--minimum-prototype-duration", type=float, default=4.0)
    parser.add_argument(
        "--prototype-scope", choices=("outside", "anywhere"), default="outside",
        help="outside requires examples outside the scored interval; anywhere uses held-out clean turns and excludes overlapping score windows",
    )
    parser.add_argument("--minimum-coverage", type=float, default=0.70)
    parser.add_argument("--minimum-score", type=float, default=0.40)
    parser.add_argument("--minimum-margin", type=float, default=0.12)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.start < 0 or args.duration <= 0 or args.window <= 0 or args.hop <= 0:
        raise ValueError("start, duration, window, and hop must be positive")

    audio_dir = args.data_root / "Eval_Ali_far" / "audio_dir"
    audio_path, resolved_recording = resolve_far_audio(audio_dir, args.recording)
    if resolved_recording != args.recording:
        raise ValueError(f"recording mismatch: requested={args.recording}, resolved={resolved_recording}")
    grid_path = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{args.recording}.TextGrid"
    info = sf.info(audio_path)
    if info.samplerate != 16000 or args.channel < 0 or args.channel >= info.channels:
        raise ValueError(f"expected 16 kHz audio and valid channel; got {info.samplerate} Hz / {info.channels} channels")
    end_s = args.start + args.duration
    if end_s > info.duration or args.window > args.duration:
        raise ValueError(f"evaluation window exceeds audio duration {info.duration:.2f}s")

    rows = intervals_for_recording(grid_path, end_s=info.duration)
    speaker_names = sorted({str(row["speaker_tier"]) for row in rows})
    provider = CampPlusSpeakerEmbeddingProvider()
    prototypes: dict[str, list[float]] = {}
    prototype_sources = {}
    started = time.perf_counter()
    window_latencies_ms = []
    with sf.SoundFile(audio_path) as audio:
        for speaker in speaker_names:
            candidates = []
            speaker_rows = [row for row in rows if row["speaker_tier"] == speaker]
            for row in speaker_rows:
                left, right = float(row["start_s"]), float(row["end_s"])
                duration = right - left
                outside_eval = right <= args.start or left >= end_s
                overlaps_other = any(
                    other["speaker_tier"] != speaker
                    and max(left, float(other["start_s"])) < min(right, float(other["end_s"]))
                    for other in rows
                )
                if (args.prototype_scope == "anywhere" or outside_eval) and duration >= args.minimum_prototype_duration and not overlaps_other:
                    candidates.append(row)
            if not candidates:
                continue
            # Prefer long examples but cap at 12 seconds to avoid heterogeneous prototypes.
            row = max(candidates, key=lambda item: min(float(item["end_s"]) - float(item["start_s"]), 12.0))
            left, right = float(row["start_s"]), float(row["end_s"])
            right = min(right, left + 12.0)
            audio.seek(round(left * info.samplerate))
            samples = audio.read(round((right - left) * info.samplerate), dtype="int16", always_2d=True)[:, args.channel]
            vector = provider.embed_audio(wav_bytes(samples, info.samplerate))
            if vector:
                prototypes[speaker] = vector
                prototype_sources[speaker] = {"start_s": round(left, 3), "end_s": round(right, 3), "text": row["text"]}
        if len(prototypes) < 2:
            raise RuntimeError(
                "at least two speakers need clean CAM++ prototypes; "
                f"found {len(prototypes)} of {len(speaker_names)} reference speakers"
            )

        windows = []
        cursor = args.start
        while cursor + args.window <= end_s + 1e-9:
            audio.seek(round(cursor * info.samplerate))
            samples = audio.read(round(args.window * info.samplerate), dtype="int16", always_2d=True)[:, args.channel]
            embed_started = time.perf_counter()
            vector = provider.embed_audio(wav_bytes(samples, info.samplerate))
            window_latencies_ms.append((time.perf_counter() - embed_started) * 1000)
            scores = {speaker: cosine(vector, proto) for speaker, proto in prototypes.items()} if vector else {}
            predicted, top_score, margin = classify_similarities(
                scores, minimum_score=args.minimum_score, minimum_margin=args.minimum_margin
            )
            reference, coverage, overlap = clean_reference_label(
                rows, cursor, cursor + args.window, minimum_coverage=args.minimum_coverage
            )
            prototype_holdout = any(
                max(cursor, source["start_s"]) < min(cursor + args.window, source["end_s"])
                for source in prototype_sources.values()
            )
            windows.append(
                {
                    "start_s": round(cursor, 3), "end_s": round(cursor + args.window, 3),
                    "predicted": predicted, "reference": reference,
                    "top_cosine": round(top_score, 4), "margin": round(margin, 4),
                    "reference_coverage": round(coverage, 4), "reference_overlap": overlap,
                    "prototype_holdout": prototype_holdout,
                    "scoring_reference": reference if reference in prototypes and not prototype_holdout else None,
                    "correct": predicted == reference if reference in prototypes and predicted is not None and not prototype_holdout else None,
                    "scores": {key: round(value, 4) for key, value in scores.items()},
                }
            )
            cursor += args.hop

    clean_windows = [row for row in windows if row["reference"] is not None and not row["prototype_holdout"]]
    eligible = [row for row in clean_windows if row["scoring_reference"] is not None]
    labeled = [row for row in eligible if row["predicted"] is not None]
    report = {
        "method": f"CAM++ cosine classification on fixed sliding windows; prototype scope={args.prototype_scope}, using clean non-overlapping TextGrid turns",
        "recording": args.recording, "audio": str(audio_path), "channel": args.channel,
        "start_s": args.start, "end_s": end_s, "window_s": args.window, "hop_s": args.hop,
        "minimum_score": args.minimum_score, "minimum_margin": args.minimum_margin,
        "minimum_reference_coverage": args.minimum_coverage, "prototype_scope": args.prototype_scope,
        "prototype_sources": prototype_sources,
        "reference_speakers_without_prototype": sorted(set(speaker_names) - set(prototypes)),
        "window_count": len(windows), "clean_reference_windows_excluding_prototypes": len(clean_windows),
        "scorable_clean_windows": len(eligible),
        "unrepresented_reference_windows": len(clean_windows) - len(eligible),
        "labeled_clean_windows": len(labeled),
        "coverage": round(len(labeled) / len(eligible), 4) if eligible else None,
        "accuracy_when_labeled": round(sum(row["correct"] for row in labeled) / len(labeled), 4) if labeled else None,
        "correct_labeled_windows": sum(bool(row["correct"]) for row in labeled),
        "ambiguous_or_unlabeled_windows": sum(row["predicted"] is None for row in eligible),
        "window_embedding_latency_ms": {
            "p50": round(percentile(window_latencies_ms, 0.50), 1),
            "p95": round(percentile(window_latencies_ms, 0.95), 1),
            "max": round(max(window_latencies_ms), 1) if window_latencies_ms else None,
        },
        "runtime_s": round(time.perf_counter() - started, 3),
        "windows": windows,
        "limitations": [
            "Offline diagnostic only; it does not modify live transcript speaker spans.",
            "Prototype selection uses TextGrid annotations and is not a speaker-discovery test; windows overlapping selected prototype turns are excluded from scoring.",
            "Overlapping or insufficiently covered windows are excluded from accuracy and counted as ambiguous.",
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
