#!/usr/bin/env python3
"""Evaluate real-time-order CAM++ clustering without reference prototypes.

Each audio window is embedded and assigned immediately to a cluster centroid
using the same StickySpeakerAssigner implementation used by the live pipeline.
TextGrid labels are read only after predictions are fixed, for permutation
mapping and scoring.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.providers.embedding import CampPlusSpeakerEmbeddingProvider
from app.services.speaker import StickySpeakerAssigner
from scripts.diagnose_ali_final_alignment import intervals_for_recording
from scripts.evaluate_ali_far_nonoverlap import resolve_far_audio
from scripts.evaluate_ali_windowed_diarization import clean_reference_label, percentile
from scripts.score_ali_far_diarization import score_der


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, samples, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def point_windows_to_spans(windows: list[dict], *, start_s: float, end_s: float) -> list[dict]:
    """Turn point-in-time window decisions into non-overlapping score spans."""
    if not windows:
        return []
    centers = [(float(row["start_s"]) + float(row["end_s"])) / 2 for row in windows]
    boundaries = [start_s]
    boundaries.extend((left + right) / 2 for left, right in zip(centers, centers[1:]))
    boundaries.append(end_s)
    spans = []
    for index, row in enumerate(windows):
        label = row.get("raw_cluster")
        if label is None:
            continue
        left, right = max(start_s, boundaries[index]), min(end_s, boundaries[index + 1])
        if right > left:
            spans.append({"start_s": left, "end_s": right, "speaker": label})
    return spans


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", required=True, help="AliMeeting ID, e.g. R8003_M8001")
    parser.add_argument("--data-root", type=Path, default=ROOT / ".local" / "data" / "datasets" / "Eval_Ali")
    parser.add_argument("--channel", type=int, default=3)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--window", type=float, default=2.0)
    parser.add_argument("--hop", type=float, default=1.0)
    parser.add_argument("--match-threshold", type=float, default=0.45)
    parser.add_argument("--min-rms", type=float, default=50.0, help="minimum int16 RMS before CAM++ clustering")
    parser.add_argument("--max-clusters", type=int, default=8)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.start < 0 or args.duration <= 0 or args.window <= 0 or args.hop <= 0:
        raise ValueError("start, duration, window, and hop must be positive")

    audio_path, _ = resolve_far_audio(args.data_root / "Eval_Ali_far" / "audio_dir", args.recording)
    info = sf.info(audio_path)
    if info.samplerate != 16000 or not 0 <= args.channel < info.channels:
        raise ValueError(f"expected 16 kHz audio and valid channel, got {info.samplerate} Hz / {info.channels} channels")
    end_s = args.start + args.duration
    if end_s > info.duration or args.window > args.duration:
        raise ValueError(f"requested range exceeds recording duration {info.duration:.3f}s")
    grid_path = args.data_root / "Eval_Ali_far" / "textgrid_dir" / f"{args.recording}.TextGrid"
    references = intervals_for_recording(grid_path, end_s=end_s)

    provider = CampPlusSpeakerEmbeddingProvider()
    assigner = StickySpeakerAssigner(
        sticky_s=45.0, min_new_ms=1000, match_threshold=args.match_threshold, max_clusters=args.max_clusters
    )
    windows: list[dict] = []
    embedding_latency_ms: list[float] = []
    started = time.perf_counter()
    with sf.SoundFile(audio_path) as audio:
        cursor = args.start
        while cursor + args.window <= end_s + 1e-9:
            audio.seek(round(cursor * info.samplerate))
            samples = audio.read(round(args.window * info.samplerate), dtype="int16", always_2d=True)[:, args.channel]
            rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64))))) if len(samples) else 0.0
            cluster = None
            latency = 0.0
            if rms >= args.min_rms:
                embedding_started = time.perf_counter()
                vector = provider.embed_audio(wav_bytes(samples, info.samplerate))
                latency = (time.perf_counter() - embedding_started) * 1000
                embedding_latency_ms.append(latency)
                if vector:
                    assigned = assigner.assign(
                        duration_ms=round(args.window * 1000),
                        now_s=cursor + args.window / 2,
                        embedding=vector,
                    )
                    cluster = assigned["speaker_label"]
            reference, coverage, overlap = clean_reference_label(
                references, cursor, cursor + args.window, minimum_coverage=0.70
            )
            windows.append({
                "start_s": round(cursor, 3), "end_s": round(cursor + args.window, 3),
                "raw_cluster": cluster, "predicted": None, "reference": reference,
                "rms_int16": round(rms, 1), "embedding_latency_ms": round(latency, 1),
                "reference_coverage": round(coverage, 4), "reference_overlap": overlap,
            })
            cursor += args.hop

    raw_spans = point_windows_to_spans(windows, start_s=args.start, end_s=end_s)
    scoring = score_der(references, raw_spans, start_s=args.start, end_s=end_s)
    mapping = scoring["speaker_mapping"]
    for row in windows:
        row["predicted"] = mapping.get(row["raw_cluster"]) if row["raw_cluster"] else None
    clean_windows = [row for row in windows if row["reference"] is not None]
    labeled_clean = [row for row in clean_windows if row["predicted"] is not None]
    correct_clean = sum(row["predicted"] == row["reference"] for row in labeled_clean)
    cluster_counts: dict[str, int] = {}
    for row in windows:
        if row["raw_cluster"]:
            cluster_counts[row["raw_cluster"]] = cluster_counts.get(row["raw_cluster"], 0) + 1

    report = {
        "method": "causal fixed-window CAM++ embeddings assigned in timestamp order to StickySpeakerAssigner; TextGrid used only after assignment for global scoring-label mapping",
        "recording": args.recording, "audio": str(audio_path), "channel": args.channel,
        "start_s": args.start, "end_s": end_s, "window_s": args.window, "hop_s": args.hop,
        "match_threshold": args.match_threshold, "min_rms_int16": args.min_rms,
        "max_clusters": args.max_clusters, "cluster_count": len(cluster_counts),
        "cluster_window_counts": cluster_counts,
        "evaluation_speaker_mapping": mapping,
        "der": scoring["der"], "missed_speaker_seconds": scoring["missed_speaker_seconds"],
        "false_alarm_speaker_seconds": scoring["false_alarm_speaker_seconds"],
        "confusion_speaker_seconds": scoring["confusion_speaker_seconds"],
        "reference_speaker_seconds": scoring["reference_speaker_seconds"],
        "clean_reference_windows": len(clean_windows), "labeled_clean_windows": len(labeled_clean),
        "correct_labeled_clean_windows": correct_clean,
        "clean_window_coverage": round(len(labeled_clean) / len(clean_windows), 4) if clean_windows else None,
        "clean_window_accuracy_when_labeled": round(correct_clean / len(labeled_clean), 4) if labeled_clean else None,
        "embedding_count": len(embedding_latency_ms),
        "embedding_cold_start_latency_ms": round(embedding_latency_ms[0], 1) if embedding_latency_ms else None,
        "embedding_steady_state_latency_ms": {
            "p50": round(value, 1) if (value := percentile(embedding_latency_ms[1:], 0.50)) is not None else None,
            "p95": round(value, 1) if (value := percentile(embedding_latency_ms[1:], 0.95)) is not None else None,
            "max": round(max(embedding_latency_ms[1:]), 1) if len(embedding_latency_ms) > 1 else None,
        },
        "embedding_latency_including_cold_start_ms": {
            "p50": round(value, 1) if (value := percentile(embedding_latency_ms, 0.50)) is not None else None,
            "p95": round(value, 1) if (value := percentile(embedding_latency_ms, 0.95)) is not None else None,
            "max": round(max(embedding_latency_ms), 1) if embedding_latency_ms else None,
        },
        "runtime_s": round(time.perf_counter() - started, 3), "windows": windows,
        "limitations": [
            "No TextGrid speaker prototypes or reference labels enter the online-order clustering decisions.",
            "Global speaker mapping uses TextGrid only after all predictions are fixed, as required for permutation-invariant scoring.",
            "Each sliding-window decision is projected to the nearest window center for DER; overlapping reference regions are scored with overlap-aware DER.",
            "This is an offline replay of the production cluster assigner, not a Socket.IO live pipeline replay.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "windows"}, ensure_ascii=False, indent=2))
    print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
