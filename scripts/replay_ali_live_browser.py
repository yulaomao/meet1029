#!/usr/bin/env python3
"""Replay AliMeeting audio through a real browser microphone and local web app.

This deliberately uses Chromium's file-backed fake microphone instead of
Flask-SocketIO's in-process test client. Audio is sent by the browser over the
same Socket.IO path as a real meeting, while the deployed speech worker runs
independently.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import soundfile as sf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from tests.e2e.browser import launch_chromium  # noqa: E402


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 2)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def health(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # health is sampled; retain the failure in the report
        return {"error": repr(exc)}


def speech_worker_sample() -> list[dict[str, int]]:
    samples = []
    for process in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "num_threads"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
            if "app.workers.speech" not in command:
                continue
            info = process.info.get("memory_info")
            samples.append(
                {
                    "pid": int(process.info["pid"]),
                    "rss_bytes": int(info.rss) if info else 0,
                    "private_bytes": int(getattr(info, "private", 0)) if info else 0,
                    "thread_count": int(process.info.get("num_threads") or 0),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return samples


def gpu_sample() -> dict[str, int] | None:
    try:
        raw = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        ).splitlines()[0]
        used, utilization = raw.split(",")
        return {"global_used_mib": int(used.strip()), "utilization_pct": int(utilization.strip())}
    except Exception:
        return None


def save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path, help="16 kHz mono PCM WAV")
    parser.add_argument("--start", type=float, default=0.0, help="clip start in source seconds")
    parser.add_argument("--duration", type=float, required=True, help="requested replay duration")
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR", ROOT / ".local" / "data")))
    parser.add_argument("--email", default=os.environ.get("LOCAL_TEST_EMAIL", "admin@local"))
    parser.add_argument("--password", default=os.environ.get("LOCAL_TEST_PASSWORD", "admin123"))
    parser.add_argument("--drain-timeout", type=float, default=180.0)
    parser.add_argument("--sample-interval", type=float, default=5.0)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    audio = args.audio.resolve()
    info = sf.info(audio)
    if info.samplerate != 16000 or info.channels != 1 or info.subtype != "PCM_16":
        raise ValueError(f"expected 16 kHz mono PCM16 WAV; got {info.samplerate} Hz, {info.channels} ch, {info.subtype}")
    if args.start < 0 or args.duration <= 0 or args.start + args.duration > info.duration:
        raise ValueError(f"clip [{args.start}, {args.start + args.duration}] exceeds {info.duration:.3f}s input")

    report: dict[str, Any] = {
        "status": "running",
        "capture_path": "Chromium file-backed virtual microphone -> AudioWorklet -> network Socket.IO -> deployed speech worker",
        "source_audio": str(audio),
        "source_range_s": [args.start, args.start + args.duration],
        "requested_audio_seconds": args.duration,
        "trailer_silence_seconds": 2.0,
        "target_pcm_bytes": int((args.duration + 2.0) * 32000),
        "samples": [],
        "partials": [],
        "page_errors": [],
    }
    audio_fd, temp_audio_name = tempfile.mkstemp(prefix="ali-browser-mic-", suffix=".wav")
    os.close(audio_fd)
    virtual_audio = Path(temp_audio_name)
    meeting_id = None
    page = None
    context = None
    browser = None
    started_at = time.monotonic()

    try:
        with sf.SoundFile(audio) as source:
            source.seek(round(args.start * info.samplerate))
            clip = source.read(round(args.duration * info.samplerate), dtype="int16", always_2d=True)
        mic_signal = np.concatenate(
            [clip[:, 0], np.zeros(round(2.0 * info.samplerate), dtype=clip.dtype)]
        )
        sf.write(virtual_audio, mic_signal, info.samplerate, subtype="PCM_16")

        from playwright.sync_api import sync_playwright

        base = args.base_url.rstrip("/")
        meeting_dir: Path | None = None
        with sync_playwright() as playwright:
            browser = launch_chromium(
                playwright,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-audio-capture={virtual_audio}",
                ],
            )
            context = browser.new_context()
            context.grant_permissions(["microphone"], origin=base)
            page = context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            page.on("console", lambda message: report["page_errors"].append(message.text) if message.type == "error" else None)
            page.goto(base + "/login")
            page.fill('input[name="email"]', args.email)
            page.fill('input[name="password"]', args.password)
            page.click('button[type="submit"]')
            page.wait_for_url("**/", timeout=20000)
            page.goto(base + "/meetings/new")
            page.fill('input[name="name"]', f"AliMeeting 浏览器实时回放 {time.strftime('%Y%m%d-%H%M%S')}")
            page.click("#review-start")
            page.click("#confirm-start")
            page.wait_for_selector("#live-root", timeout=20000)
            meeting_id = page.locator("#live-root").get_attribute("data-meeting-id")
            if not meeting_id:
                raise RuntimeError("live page did not expose a meeting ID")
            report["meeting_id"] = meeting_id
            meeting_dir = args.data_dir.resolve() / "meetings" / meeting_id
            spool_path = meeting_dir / "realtime.pcm"
            cursor_path = meeting_dir / "speech_cursor.json"

            target_bytes = report["target_pcm_bytes"]
            receive_deadline = time.monotonic() + args.duration * 1.5 + 60
            next_sample = time.monotonic()
            last_partial = ""
            reached_target_at: float | None = None
            final_display_count = 0
            final_connection_state = ""
            while time.monotonic() < receive_deadline:
                now = time.monotonic()
                received_bytes = spool_path.stat().st_size if spool_path.exists() else 0
                cursor = read_json(cursor_path)
                queue_lag_ms = int(cursor.get("asr_queue_lag_ms") or 0)
                processed_bytes = int(cursor.get("pcm_offset") or 0)
                partial_text = page.locator("article.seg.partial-live p").inner_text() if page.locator("article.seg.partial-live p").count() else ""
                if partial_text and partial_text != last_partial:
                    report["partials"].append({"elapsed_s": round(now - started_at, 2), "text": partial_text})
                    last_partial = partial_text

                if now >= next_sample:
                    health_result = health(base)
                    sample = {
                        "elapsed_s": round(now - started_at, 1),
                        "received_pcm_bytes": received_bytes,
                        "received_audio_s": round(received_bytes / 32000, 2),
                        "processed_pcm_bytes": processed_bytes,
                        "processed_audio_s": round(processed_bytes / 32000, 2),
                        "queue_lag_ms": queue_lag_ms,
                        "asr_lag_label": page.locator("#asr-lag").inner_text() if page.locator("#asr-lag").count() else "",
                        "asr_lag_title": page.locator("#asr-lag").get_attribute("title") if page.locator("#asr-lag").count() else "",
                        "threaded_speech_workers": speech_worker_sample(),
                        "gpu": gpu_sample(),
                        "health": health_result,
                    }
                    report["samples"].append(sample)
                    save_report(args.report, report)
                    next_sample = now + max(1.0, args.sample_interval)
                    final_display_count = page.locator("#transcript-list article.seg:not(.partial-live)").count()

                if received_bytes >= target_bytes and reached_target_at is None:
                    reached_target_at = now
                    report["target_audio_received_at_s"] = round(now - started_at, 2)
                    final_display_count = page.locator("#transcript-list article.seg:not(.partial-live)").count()
                    final_connection_state = page.locator("#conn-state").inner_text()
                    break
                if not page.locator("#live-root").count():
                    raise RuntimeError("live page closed before the requested PCM reached the server")
                page.wait_for_timeout(250)

            if page.locator("#end-meeting").count():
                page.click("#end-meeting")
                page.wait_for_url("**/meetings", timeout=20000)
                report["meeting_ended_via_ui"] = True

            # Stop the microphone immediately at the target receive duration,
            # then let the independent speech worker drain the queued spool.
            drain_deadline = time.monotonic() + args.drain_timeout
            while time.monotonic() < drain_deadline:
                now = time.monotonic()
                received_bytes = spool_path.stat().st_size if spool_path.exists() else 0
                cursor = read_json(cursor_path)
                queue_lag_ms = int(cursor.get("asr_queue_lag_ms") or 0)
                processed_bytes = int(cursor.get("pcm_offset") or 0)
                if now >= next_sample:
                    report["samples"].append(
                        {
                            "elapsed_s": round(now - started_at, 1),
                            "received_pcm_bytes": received_bytes,
                            "received_audio_s": round(received_bytes / 32000, 2),
                            "processed_pcm_bytes": processed_bytes,
                            "processed_audio_s": round(processed_bytes / 32000, 2),
                            "queue_lag_ms": queue_lag_ms,
                            "threaded_speech_workers": speech_worker_sample(),
                            "gpu": gpu_sample(),
                            "health": health(base),
                            "phase": "drain",
                        }
                    )
                    save_report(args.report, report)
                    next_sample = now + max(1.0, args.sample_interval)
                if processed_bytes >= min(target_bytes, received_bytes) and queue_lag_ms < 100:
                    break
                time.sleep(0.5)

            received_bytes = spool_path.stat().st_size if spool_path.exists() else 0
            cursor = read_json(cursor_path)
            processed_bytes = int(cursor.get("pcm_offset") or 0)
            drained = (
                reached_target_at is not None
                and received_bytes >= target_bytes
                and processed_bytes >= target_bytes
                and int(cursor.get("asr_queue_lag_ms") or 0) < 100
            )
            report.update(
                {
                    "status": "completed" if drained else "incomplete_backlog",
                    "actual_wall_seconds": round(time.monotonic() - started_at, 2),
                    "received_pcm_bytes": received_bytes,
                    "received_audio_seconds": round(received_bytes / 32000, 2),
                    "received_content_audio_seconds": round(min(args.duration, received_bytes / 32000), 2),
                    "server_processed_pcm_bytes": processed_bytes,
                    "server_processed_audio_seconds": round(processed_bytes / 32000, 2),
                    "final_queue_lag_ms": int(cursor.get("asr_queue_lag_ms") or 0),
                    "final_display_count": final_display_count,
                    "final_connection_state": final_connection_state,
                    "drained": drained,
                }
            )
            if reached_target_at is None:
                report["status"] = "incomplete_delivery"
                report["delivery_deadline_s"] = round(args.duration * 1.5 + 60, 2)
            queue_samples = [float(sample.get("queue_lag_ms") or 0) for sample in report["samples"]]
            partial_times = [float(item["elapsed_s"]) for item in report["partials"]]
            report["queue_lag_p95_ms"] = percentile(queue_samples, 0.95)
            report["partial_gap_p95_s"] = percentile(
                [right - left for left, right in zip(partial_times, partial_times[1:])], 0.95
            )
            try:
                report["persisted_transcript"] = page.evaluate(
                    "async id => { const r = await fetch('/api/meetings/' + id + '/transcript'); "
                    "return r.ok ? await r.json() : {http_status: r.status}; }",
                    meeting_id,
                )
                report["final_display_count"] = len(report["persisted_transcript"].get("segments", []))
            except Exception as exc:
                report["transcript_fetch_error"] = repr(exc)

            report["post_stop_health"] = health(base)

    except Exception as exc:
        report["status"] = "error"
        report["error"] = repr(exc)
        report["actual_wall_seconds"] = round(time.monotonic() - started_at, 2)
        if meeting_id:
            report["meeting_id"] = meeting_id
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        virtual_audio.unlink(missing_ok=True)
        save_report(args.report, report)

    print(json.dumps({key: report.get(key) for key in ("status", "meeting_id", "requested_audio_seconds", "received_audio_seconds", "server_processed_audio_seconds", "final_queue_lag_ms", "final_display_count", "final_connection_state", "page_errors", "error")}, ensure_ascii=False))
    return 0 if report.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
