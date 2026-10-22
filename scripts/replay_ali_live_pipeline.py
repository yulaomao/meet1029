#!/usr/bin/env python3
"""Replay AliMeeting audio through Socket.IO ingress and the live worker loop.

Runs in an isolated SQLite app and temporary data directory. It exercises the
same 40 ms PCM16 ingress, spool, speech worker, VAD, SmartTurn, and ASR event
path as a local meeting without touching the deployed database.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["FLASK_ENV"] = "testing"
load_dotenv(ROOT / ".env")

from app.config import TestConfig  # noqa: E402
from app.models import Meeting, MeetingStatus, Terminology, TranscriptSegment, User  # noqa: E402
from app.providers.diarization import FakeDiarizationProvider  # noqa: E402
from app.providers.embedding import CampPlusSpeakerEmbeddingProvider, FakeEmbeddingProvider  # noqa: E402
from app.realtime.smart_turn import get_smart_turn  # noqa: E402
from app.services.speech_pipeline import SpeechPipeline  # noqa: E402
from app.web.extensions import db, socketio  # noqa: E402
from app.web.factory import create_app, seed_users  # noqa: E402
from app.web.socket_events import NS  # noqa: E402
from scripts.evaluate_ali_far_nonoverlap import resolve_far_audio  # noqa: E402
from scripts.evaluate_ali_realtime import normalize, read_intervals, reference_until  # noqa: E402


DEFAULT_ROOT = ROOT / ".local" / "data" / "datasets" / "Eval_Ali"


def resolve_replay_audio(data_root: Path, track: str, recording: str, speaker: str) -> tuple[Path, str]:
    """Resolve a replay clip and meeting ID without guessing far-field mic IDs."""
    if track == "near":
        meeting = recording.removesuffix("_MS801")
        audio = data_root / "Eval_Ali_near" / "audio_dir" / f"{meeting}_N_{speaker}.wav"
        return audio, meeting
    return resolve_far_audio(data_root / "Eval_Ali_far" / "audio_dir", recording)


def percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    values.sort()
    return values[min(len(values) - 1, round((len(values) - 1) * p))]


def streaming_asr_settings(provider) -> dict:
    """Return the effective streaming decoder settings for reproducible reports."""
    return {
        "model": getattr(provider, "model_id", None),
        "device": getattr(provider, "device", None),
        "chunk_ms": getattr(provider, "chunk_ms", None),
        "stable_tail_chars": getattr(provider, "stable_tail_chars", None),
        "encoder_chunk_look_back": getattr(provider, "encoder_chunk_look_back", None),
    }


def coalesce_final_events(finals: list[dict]) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    """Keep first-final latency and latest corrected text per transcript row."""
    first: dict[str, dict] = {}
    latest: dict[str, dict] = {}
    for item in finals:
        data = item["data"]
        segment_id = str(data.get("id") or f"{data.get('utterance_id')}:{data.get('turn_revision')}")
        first.setdefault(segment_id, item)
        latest[segment_id] = item
    ordered = sorted(
        latest.values(),
        key=lambda item: (int(item["data"].get("start_ms") or 0), float(item["wall_s"])),
    )
    return first, latest, ordered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--recording", default="R8001_M8004_MS801")
    parser.add_argument("--track", choices=("far", "near"), default="far")
    parser.add_argument("--speaker", default="SPK8013", help="near-field speaker ID, e.g. SPK8013")
    parser.add_argument("--vad-threshold", type=float, help="override EnergyVAD RMS threshold for this replay")
    parser.add_argument(
        "--speaker-clustering",
        action="store_true",
        help="run local CAM++ embeddings on completed VAD turns; this is utterance clustering, not streaming diarization",
    )
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=90.0,
        help="extra seconds after real-time playback for endpoint/final processing to drain",
    )
    parser.add_argument("--channel", help="select one channel index; use 'mean' only after checking phase correlation")
    parser.add_argument("--tier", help="score against one TextGrid speaker tier, e.g. N_SPK8013")
    parser.add_argument(
        "--terminology",
        action="append",
        default=[],
        help="seed one global ASR hotword in the isolated replay database; repeat for multiple terms",
    )
    parser.add_argument("--all-reference-tiers", action="store_true", help="include every speaker tier in the reference")
    parser.add_argument(
        "--sensevoice-refinement",
        action="store_true",
        help="enable and measure the configured asynchronous SenseVoice final refinement",
    )
    parser.add_argument("--fast", action="store_true", help="send frames without real-time pacing")
    parser.add_argument("--report", type=Path, default=ROOT / ".local" / "evals" / "ali-live-pipeline.json")
    args = parser.parse_args()
    if args.vad_threshold is not None:
        os.environ["VAD_ENERGY_THRESHOLD"] = str(args.vad_threshold)

    default_tier = f"N_{args.speaker}" if args.track == "near" else None
    reference_tier = None if args.all_reference_tiers else (args.tier or default_tier)
    audio_path, recording = resolve_replay_audio(args.data_root, args.track, args.recording, args.speaker)
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

    run_dir = args.report.parent / (args.report.stem + "-runtime")
    run_dir.mkdir(parents=True, exist_ok=True)
    data_dir = run_dir / "data"

    class ReplayConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite://"
        DATA_DIR = data_dir
        SPEECH_BACKEND = "funasr"
        LLM_BACKEND = "fake"
        TESTING = True
        SMART_TURN_ENABLED = True
        SMART_TURN_MODEL_PATH = str(ROOT / ".local" / "models" / "smart-turn-v3.2-cpu.onnx")
        SENSEVOICE_REFINEMENT_ENABLED = bool(args.sensevoice_refinement)
        SPEAKER_ID_MIN_MS = 1500 if args.speaker_clustering else 999999
        ROLLING_SUMMARY_MIN_SEGMENTS = 999999
        REALTIME_BACKGROUND_LLM_ENABLED = False
        INTERNAL_LIVE_PORT = 5001

    app = create_app(ReplayConfig, SQLALCHEMY_DATABASE_URI=ReplayConfig.SQLALCHEMY_DATABASE_URI, DATA_DIR=data_dir)
    providers = app.extensions["providers"]
    speaker_embedding_stats = {"calls": 0, "succeeded": 0, "failed": 0, "latency_ms": []}
    if args.speaker_clustering:
        if not isinstance(providers.embedding, CampPlusSpeakerEmbeddingProvider):
            raise RuntimeError("local CAM++ speaker embeddings are not configured")
        if not Path(providers.embedding.model_id).exists():
            raise RuntimeError(f"local CAM++ speaker model is missing: {providers.embedding.model_id}")
        original_embed_audio = providers.embedding.embed_audio
        embedding_stats_lock = threading.Lock()

        def tracked_embed_audio(data: bytes, **kwargs):
            started = time.perf_counter()
            with embedding_stats_lock:
                speaker_embedding_stats["calls"] += 1
            try:
                result = original_embed_audio(data, **kwargs)
                with embedding_stats_lock:
                    if result:
                        speaker_embedding_stats["succeeded"] += 1
                    else:
                        speaker_embedding_stats["failed"] += 1
                return result
            except Exception:
                with embedding_stats_lock:
                    speaker_embedding_stats["failed"] += 1
                raise
            finally:
                with embedding_stats_lock:
                    speaker_embedding_stats["latency_ms"].append(round((time.perf_counter() - started) * 1000))

        providers.embedding.embed_audio = tracked_embed_audio
    else:
        providers.diarization = FakeDiarizationProvider()
        providers.embedding = FakeEmbeddingProvider()
    if not providers.asr.model_id or not Path(providers.asr.model_id).exists():
        raise RuntimeError("FUNASR_STREAMING_MODEL must point to the already downloaded local Paraformer model")
    if not Path(app.config["SMART_TURN_MODEL_PATH"]).is_file():
        raise RuntimeError(f"SmartTurn model is missing: {app.config['SMART_TURN_MODEL_PATH']}")
    if args.sensevoice_refinement and providers.sensevoice is None:
        raise RuntimeError("SenseVoice refinement requested but SENSEVOICE_REFINEMENT_ENABLED is false")

    with app.app_context():
        db.create_all()
        seed_users()
        admin = User.query.filter_by(email="admin@local").one()
        meeting = Meeting(
            name="AliMeeting real-time replay",
            created_by=admin.id,
            status=MeetingStatus.RECORDING.value,
            consent_given=True,
        )
        db.session.add(meeting)
        terminology = dict.fromkeys(
            str(value).strip() for value in args.terminology if str(value).strip()
        )
        for term in terminology:
            db.session.add(Terminology(term=term, enabled=True))
        db.session.commit()
        meeting_id = meeting.id
        store = app.extensions["audio_store"]
        pcm_path = store.meeting_dir(meeting_id) / "realtime.pcm"
        report_events: list[dict] = []
        timing = {"started": 0.0, "sent_samples": 0}
        timing_lock = threading.Lock()

        import app.services.speech_pipeline as speech_pipeline

        original_broadcast = speech_pipeline.broadcast_live
        original_spawn = SpeechPipeline._spawn_ai_turn
        original_correction = SpeechPipeline._queue_correction
        original_refinement = SpeechPipeline._queue_sensevoice_refinement
        refinement_state = {"queued": 0, "completed": 0}

        if args.sensevoice_refinement:
            providers.sensevoice._load()
            original_transcribe_pcm = providers.sensevoice.transcribe_pcm
            refinement_lock = threading.Lock()

            def tracked_transcribe_pcm(pcm16: bytes, *, sample_rate: int = 16000) -> str:
                try:
                    return original_transcribe_pcm(pcm16, sample_rate=sample_rate)
                finally:
                    with refinement_lock:
                        refinement_state["completed"] += 1

            providers.sensevoice.transcribe_pcm = tracked_transcribe_pcm

            def tracked_refinement(self, _app, _meeting_id, _segment_id, pcm, *, sample_rate, duration_ms):
                minimum = max(1, int(_app.config.get("SENSEVOICE_MIN_UTTERANCE_MS", 700)))
                maximum = max(minimum, int(_app.config.get("SENSEVOICE_MAX_UTTERANCE_MS", 30000)))
                if pcm and minimum <= duration_ms <= maximum:
                    with refinement_lock:
                        refinement_state["queued"] += 1
                return original_refinement(
                    self,
                    _app,
                    _meeting_id,
                    _segment_id,
                    pcm,
                    sample_rate=sample_rate,
                    duration_ms=duration_ms,
                )

            SpeechPipeline._queue_sensevoice_refinement = tracked_refinement

        def capture_broadcast(_app, event: str, _meeting_id: str, payload: dict) -> None:
            with timing_lock:
                sent_s = timing["sent_samples"] / info.samplerate
                wall_s = time.perf_counter() - timing["started"]
            report_events.append(
                {"event": event, "data": dict(payload), "sent_s": sent_s, "wall_s": wall_s}
            )
            original_broadcast(_app, event, _meeting_id, payload)

        speech_pipeline.broadcast_live = capture_broadcast
        # Keep this replay focused on speech recognition and turn boundaries;
        # external LLM calls, correction, and speaker identity are separate plan layers.
        SpeechPipeline._spawn_ai_turn = lambda self, *a, **k: None
        SpeechPipeline._queue_correction = lambda self, *a, **k: None
        if not args.sensevoice_refinement:
            SpeechPipeline._queue_sensevoice_refinement = lambda self, *a, **k: None

        client = socketio.test_client(app, namespace=NS)
        if not client.is_connected(NS):
            raise RuntimeError("Socket.IO test client failed to connect to /live")
        client.emit("join_meeting", {"meeting_id": meeting_id, "client_id": "ali-replay"}, namespace=NS)

        warm_started = time.perf_counter()
        providers.asr._load()
        providers.punctuation._load_online()
        providers.punctuation._load_final()
        analyzer = get_smart_turn(app)
        if analyzer is None:
            raise RuntimeError("SmartTurn failed to initialize with SMART_TURN_ENABLED=1")
        analyzer.predict_pcm16(bytes(16000 * 2))
        model_load_ms = round((time.perf_counter() - warm_started) * 1000, 1)

        producer_state = {"done": False, "error": None}
        frame_samples = 640  # 40 ms, 1,280-byte PCM16 frames like live.js.
        body_samples = round(args.duration * info.samplerate)
        trailer_samples = round(2.0 * info.samplerate)
        playback_started = time.perf_counter()
        with timing_lock:
            timing["started"] = playback_started

        def produce() -> None:
            try:
                with sf.SoundFile(audio_path) as wav:
                    wav.seek(round(args.start * info.samplerate))
                    sent = 0
                    while sent < body_samples:
                        count = min(frame_samples, body_samples - sent)
                        samples = wav.read(count, dtype="float32", always_2d=True)
                        if not len(samples):
                            break
                        mono = samples[:, channel] if channel is not None else samples.mean(axis=1)
                        pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                        client.emit("audio_frame", pcm, namespace=NS)
                        sent += len(samples)
                        with timing_lock:
                            timing["sent_samples"] = sent
                        if not args.fast:
                            deadline = playback_started + sent / info.samplerate
                            delay = deadline - time.perf_counter()
                            if delay > 0:
                                time.sleep(delay)
                sent_trailer = 0
                silence = bytes(frame_samples * 2)
                while sent_trailer < trailer_samples:
                    client.emit("audio_frame", silence, namespace=NS)
                    sent_trailer += frame_samples
                    with timing_lock:
                        timing["sent_samples"] += frame_samples
                    if not args.fast:
                        deadline = playback_started + (body_samples + sent_trailer) / info.samplerate
                        delay = deadline - time.perf_counter()
                        if delay > 0:
                            time.sleep(delay)
            except Exception as exc:  # surfaced on the worker thread boundary
                producer_state["error"] = repr(exc)
            finally:
                producer_state["done"] = True

        producer = threading.Thread(target=produce, name="ali-replay-producer", daemon=True)
        producer.start()
        max_runtime = (args.duration + args.drain_timeout) if not args.fast else 180
        deadline = time.perf_counter() + max_runtime
        pipeline = SpeechPipeline(app)
        last_queue_lag = 0
        last_processing = 0
        next_snapshot = 5.0
        snapshots: list[dict] = []
        seen_event_count = 0
        try:
            while time.perf_counter() < deadline:
                pipeline.process_pending(app)
                cursor = store.read_json(meeting_id, "speech_cursor.json") or {}
                offset = int(cursor.get("pcm_offset") or 0)
                file_size = pcm_path.stat().st_size if pcm_path.exists() else 0
                queue_lag_s = max(0.0, (file_size - offset) / (info.samplerate * 2))
                for item in report_events[seen_event_count:]:
                    if item["event"] == "asr_metrics":
                        last_queue_lag = int(item["data"].get("queue_lag_ms") or 0)
                        last_processing = int(item["data"].get("processing_ms") or 0)
                seen_event_count = len(report_events)

                with timing_lock:
                    sent_s = timing["sent_samples"] / info.samplerate
                effective_s = max(0.0, sent_s - queue_lag_s)
                while effective_s >= next_snapshot and next_snapshot <= args.duration:
                    latest = build_live_hypothesis(report_events)
                    reference = reference_until(
                            read_intervals(grid_path, reference_tier), args.start, args.start + next_snapshot
                    )
                    snapshots.append(
                        {
                            "audio_s": round(next_snapshot, 1),
                            "effective_audio_s": round(effective_s, 2),
                            "queue_lag_ms": round(queue_lag_s * 1000),
                            "reference_chars": len(reference),
                            "hypothesis_chars": len(normalize(latest)),
                            "rolling_cer": round(float(__import__("jiwer").cer(reference, normalize(latest))), 4)
                            if reference
                            else None,
                        }
                    )
                    next_snapshot += 5.0

                if producer_state["error"]:
                    raise RuntimeError(f"replay producer failed: {producer_state['error']}")
                session = app.extensions.get("speech_sessions", {}).get(meeting_id, {})
                if (
                    producer_state["done"]
                    and offset >= file_size
                    and not session.get("speech_active")
                    and session.get("pending_endpoint") is None
                ):
                    break
                time.sleep(0.02)
            else:
                raise TimeoutError("speech worker did not drain the real-time replay before timeout")
            producer.join(timeout=1)
            if args.sensevoice_refinement:
                refinement_deadline = time.perf_counter() + 180
                while time.perf_counter() < refinement_deadline:
                    with refinement_lock:
                        refinement_done = refinement_state["completed"] >= refinement_state["queued"]
                    if refinement_done:
                        break
                    time.sleep(0.1)
            # Give the final Socket.IO event queue a chance to settle.
            time.sleep(0.1)
            received = client.get_received(NS)

            segments = (
                TranscriptSegment.query.filter_by(meeting_id=meeting_id)
                .order_by(TranscriptSegment.start_ms.asc(), TranscriptSegment.created_at.asc())
                .all()
            )
            raw_hypothesis = normalize("".join(seg.raw_text or "" for seg in segments))
            hypothesis = normalize("".join(seg.final_text or seg.display_text or seg.raw_text or "" for seg in segments))
            refined_hypothesis = normalize(
                "".join(
                    seg.final_text or seg.display_text or seg.punctuated_text or seg.raw_text or ""
                    for seg in segments
                )
            )
            reference = reference_until(
                read_intervals(grid_path, reference_tier), args.start, args.start + args.duration
            )
            queue_samples = [
                int(item["data"].get("queue_lag_ms") or 0)
                for item in report_events
                if item["event"] == "asr_metrics"
            ]
            queue_start_window = [
                int(item["data"].get("queue_lag_ms") or 0)
                for item in report_events
                if item["event"] == "asr_metrics" and float(item["sent_s"]) <= min(30.0, args.duration)
            ]
            queue_end_window = [
                int(item["data"].get("queue_lag_ms") or 0)
                for item in report_events
                if item["event"] == "asr_metrics"
                and float(item["sent_s"]) >= max(0.0, args.duration - 30.0)
            ]
            processing_samples = [
                int(item["data"].get("processing_ms") or 0)
                for item in report_events
                if item["event"] == "asr_metrics"
            ]
            sequence_values = [
                int(item["data"]["sequence"])
                for item in report_events
                if item["event"] in {"transcript_partial", "transcript_final"}
                and item["data"].get("sequence") is not None
            ]
            sequence_regressions = sum(b <= a for a, b in zip(sequence_values, sequence_values[1:]))
            partials = [item for item in report_events if item["event"] == "transcript_partial"]
            finals = [item for item in report_events if item["event"] == "transcript_final"]
            partial_state: dict[tuple[int, int], str] = {}
            partial_prefix_regressions = 0
            for item in partials:
                data = item["data"]
                key = (
                    int(data.get("utterance_id") or 0),
                    int(data.get("turn_revision") or 0),
                )
                current = normalize(str(data.get("text") or ""))
                previous = partial_state.get(key, "")
                if previous and current and not current.startswith(previous):
                    partial_prefix_regressions += 1
                partial_state[key] = current
            first_final_by_segment, _latest_final_by_segment, latest_finals = coalesce_final_events(finals)
            text_final_events = [item for item in finals if not (item["data"] or {}).get("speaker_update")]
            _, latest_text_final_by_segment, _ = coalesce_final_events(text_final_events)
            final_event_text = normalize("".join(str(item["data"].get("text") or "") for item in latest_finals))
            persisted_speakers = {str(segment.id): str(segment.speaker_label or "Speaker 1") for segment in segments}
            event_speakers = {
                str(item["data"].get("id")): str(item["data"].get("speaker") or "Speaker 1")
                for item in latest_finals
                if item["data"].get("id")
            }
            speaker_counts: dict[str, int] = {}
            for item in latest_finals:
                label = str(item["data"].get("speaker") or "Speaker 1")
                speaker_counts[label] = speaker_counts.get(label, 0) + 1
            partial_times: dict[tuple[int, int], list[float]] = {}
            for item in partials:
                data = item["data"]
                key = (int(data.get("utterance_id") or 0), int(data.get("turn_revision") or 0))
                partial_times.setdefault(key, []).append(float(item["wall_s"]))
            partial_gaps_ms = [
                round((b - a) * 1000)
                for times in partial_times.values()
                for a, b in zip(times, times[1:])
            ]
            initial_finals = list(first_final_by_segment.values())
            final_latencies_ms = [
                max(0, round((float(item["wall_s"]) - int(item["data"].get("end_ms") or 0) / 1000) * 1000))
                for item in initial_finals
            ]
            refinement_latencies_ms = [
                max(0, round((float(item["wall_s"]) - float(first_final_by_segment[key]["wall_s"])) * 1000))
                for key, item in latest_text_final_by_segment.items()
                if float(item["wall_s"]) > float(first_final_by_segment[key]["wall_s"])
            ]
            speaker_update_latencies_ms = [
                max(0, round((float(item["wall_s"]) - float(first_final_by_segment[key]["wall_s"])) * 1000))
                for item in finals
                if (item["data"] or {}).get("speaker_update")
                for key in [str(item["data"].get("id") or f"{item['data'].get('utterance_id')}:{item['data'].get('turn_revision')}")]
                if key in first_final_by_segment
            ]
            endpoints = [item for item in report_events if item["event"] == "turn_endpoint"]
            errors = [item for item in report_events if item["event"] == "error"]
            selected_refs = read_intervals(grid_path, reference_tier)
            first_speech = next((left for left, _right, _text in selected_refs if left >= args.start), None)
            first_partial_ms = (
                round(max(0.0, partials[0]["wall_s"] - (first_speech - args.start)) * 1000, 1)
                if partials and first_speech is not None
                else None
            )
            first_sustained_speech = next(
                (
                    left
                    for left, right, _text in selected_refs
                    if left >= args.start and right - left >= 1.0
                ),
                None,
            )
            sustained_partial = next(
                (
                    item
                    for item in partials
                    if first_sustained_speech is not None
                    and item["sent_s"] >= first_sustained_speech - args.start
                ),
                None,
            )
            sustained_partial_ms = (
                round(
                    max(
                        0.0,
                        sustained_partial["wall_s"]
                        - (first_sustained_speech - args.start),
                    )
                    * 1000,
                    1,
                )
                if sustained_partial is not None and first_sustained_speech is not None
                else None
            )
            speech_session = app.extensions.get("speech_sessions", {}).get(meeting_id, {})
            asr_session = speech_session.get("asr")
            asr_hotword_count = len(str(getattr(asr_session, "hotwords", "") or "").split())
            result = {
                "dataset": f"AliMeeting Eval / {args.track}-field",
                "reference_tier": reference_tier or "all tiers (exploratory only; overlapping speech is not a valid single-stream reference)",
                "recording": audio_path.name,
                "channel_policy": "mean of all channels (explicit; phase must be validated)" if channel is None else f"channel {channel}",
                "start_s": args.start,
                "duration_s": args.duration,
                "replay_mode": "real-time Socket.IO 40 ms PCM16 frames" if not args.fast else "fast Socket.IO replay",
                "device": providers.asr.device,
                "streaming_asr_settings": streaming_asr_settings(providers.asr),
                "asr_hotword_count": asr_hotword_count,
                "vad_energy_threshold": getattr(providers.vad, "threshold", None),
                "model_warmup_ms": model_load_ms,
                "first_partial_from_first_annotated_speech_ms": first_partial_ms,
                "first_sustained_reference_speech_latency_ms": sustained_partial_ms,
                "partial_event_count": len(partials),
                "partial_update_gap_p95_ms": percentile(partial_gaps_ms, 0.95),
                "final_event_count": len(latest_finals),
                "raw_final_event_count": len(finals),
                "final_event_update_count": len(finals) - len(latest_finals),
                "final_latency_p95_ms": percentile(final_latencies_ms, 0.95),
                "final_refinement_latency_p95_ms": percentile(refinement_latencies_ms, 0.95),
                "speaker_update_event_count": sum(bool((item["data"] or {}).get("speaker_update")) for item in finals),
                "speaker_assignment_latency_p95_ms": percentile(speaker_update_latencies_ms, 0.95),
                "persisted_final_segment_count": len(segments),
                "partial_prefix_regressions": partial_prefix_regressions,
                "discarded_regressive_partial_count": int(session.get("discarded_regressive_partial_count") or 0),
                "final_event_chars": len(final_event_text),
                "final_event_matches_persisted": final_event_text == hypothesis,
                "final_speakers_match_persisted": all(
                    persisted_speakers.get(segment_id) == label
                    for segment_id, label in event_speakers.items()
                ) and set(event_speakers) == set(persisted_speakers),
                "endpoint_event_count": len(endpoints),
                "reopen_event_count": sum(item["event"] == "transcript_reopen" for item in report_events),
                "sequence_regressions": sequence_regressions,
                "socket_client_received_events": len(received),
                "queue_lag_max_ms": max(queue_samples) if queue_samples else last_queue_lag,
                "queue_lag_p95_ms": percentile(queue_samples, 0.95),
                "queue_lag_first30s_p95_ms": percentile(queue_start_window, 0.95),
                "queue_lag_last30s_p95_ms": percentile(queue_end_window, 0.95),
                "queue_lag_last_minus_first30s_ms": (
                    percentile(queue_end_window, 0.95) - percentile(queue_start_window, 0.95)
                    if queue_start_window and queue_end_window
                    else None
                ),
                "processing_p95_ms": percentile(processing_samples, 0.95),
                "reference_chars": len(reference),
                "final_chars": len(hypothesis),
                "final_cer": round(float(__import__("jiwer").cer(reference, hypothesis)), 4)
                if reference
                else None,
                "paraformer_raw_cer": round(float(__import__("jiwer").cer(reference, raw_hypothesis)), 4)
                if reference
                else None,
                "sensevoice_refined_cer": round(float(__import__("jiwer").cer(reference, refined_hypothesis)), 4)
                if reference
                else None,
                "sensevoice_refinement_enabled": bool(args.sensevoice_refinement),
                "sensevoice_min_similarity": float(app.config.get("SENSEVOICE_MIN_SIMILARITY", 0.28)),
                "sensevoice_min_utterance_ms": int(app.config.get("SENSEVOICE_MIN_UTTERANCE_MS", 700)),
                "sensevoice_refinement_queued_count": refinement_state["queued"],
                "sensevoice_refinement_completed_count": refinement_state["completed"],
                "sensevoice_refined_segment_count": sum(bool(seg.sensevoice_text) for seg in segments),
                "sensevoice_refinement_complete": refinement_state["completed"] >= refinement_state["queued"],
                "speaker_clustering_enabled": bool(args.speaker_clustering),
                "speaker_embedding_provider": type(providers.embedding).__name__,
                "speaker_embedding_model": getattr(providers.embedding, "model_id", None),
                "speaker_embedding_model_loaded": bool(getattr(providers.embedding, "_pipeline", None)),
                "speaker_embedding_inference_count": speaker_embedding_stats["calls"],
                "speaker_embedding_success_count": speaker_embedding_stats["succeeded"],
                "speaker_embedding_failure_count": speaker_embedding_stats["failed"],
                "speaker_embedding_latency_p95_ms": percentile(speaker_embedding_stats["latency_ms"], 0.95),
                "speaker_label_counts": speaker_counts,
                "final_speakers": [
                    {
                        "segment_id": item["data"].get("id"),
                        "start_ms": int(item["data"].get("start_ms") or 0),
                        "end_ms": int(item["data"].get("end_ms") or 0),
                        "speaker": item["data"].get("speaker") or "Speaker 1",
                    }
                    for item in latest_finals
                ],
                "rolling_snapshots": snapshots,
                "errors": errors,
                "reference": reference,
                "hypothesis": hypothesis,
            }
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            events_path = args.report.with_suffix(".events.jsonl")
            events_path.write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in report_events),
                encoding="utf-8",
            )
            print(json.dumps({k: v for k, v in result.items() if k not in {"reference", "hypothesis"}}, ensure_ascii=False, indent=2))
            print(f"report: {args.report.resolve()}")
            print(f"events: {events_path.resolve()}")
        finally:
            producer.join(timeout=2)
            if client.is_connected(NS):
                client.disconnect(namespace=NS)
            speech_pipeline.broadcast_live = original_broadcast
            SpeechPipeline._spawn_ai_turn = original_spawn
            SpeechPipeline._queue_correction = original_correction
            SpeechPipeline._queue_sensevoice_refinement = original_refinement
            db.session.remove()
            db.drop_all()


def build_live_hypothesis(events: list[dict]) -> str:
    finals: dict[str, dict] = {}
    partials: dict[tuple[int, int], dict] = {}
    highest_revision: dict[int, int] = {}
    for item in events:
        event, data = item["event"], item["data"]
        try:
            utterance = int(data.get("utterance_id") or data.get("turn_id") or 0)
            revision = int(data.get("turn_revision") or data.get("revision") or 0)
        except (ValueError, TypeError):
            continue
        if event == "transcript_reopen":
            highest_revision[utterance] = max(highest_revision.get(utterance, 0), revision)
            for key in [key for key in partials if key[0] == utterance and key[1] < revision]:
                partials.pop(key, None)
            for key, final in list(finals.items()):
                if int(final.get("utterance_id") or 0) == utterance:
                    finals.pop(key, None)
        elif event == "transcript_partial":
            highest_revision[utterance] = max(highest_revision.get(utterance, 0), revision)
            for key in [key for key in partials if key[0] == utterance and key[1] < revision]:
                partials.pop(key, None)
            partials[(utterance, revision)] = data
            for key, final in list(finals.items()):
                if int(final.get("utterance_id") or 0) == utterance:
                    finals.pop(key, None)
        elif event == "transcript_final":
            segment_id = str(data.get("id") or f"{utterance}:{revision}")
            finals[segment_id] = data
            if utterance:
                for key in [key for key in partials if key[0] == utterance]:
                    partials.pop(key, None)
    entries = []
    for data in finals.values():
        entries.append((int(data.get("start_ms") or 0), str(data.get("text") or "")))
    for data in partials.values():
        entries.append((10**12, str(data.get("text") or "")))
    entries.sort(key=lambda row: row[0])
    return "".join(text for _start, text in entries)


if __name__ == "__main__":
    main()
