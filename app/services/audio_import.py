"""Durable, local-only audio-file imports for completed meeting records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.graphs.post_meeting import PostMeetingGraph
from app.media import resolve_ffmpeg, resolve_ffprobe
from app.models import (
    ActionItem,
    Decision,
    KnowledgeDocument,
    KnowledgeChunk,
    Meeting,
    MeetingImportJob,
    MeetingSummary,
    TranscriptSegment,
)
from app.models.enums import MeetingStatus, TranscriptSource
from app.services.meeting import MeetingService
from app.web.extensions import db


ALLOWED_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm"}
_COPY_BLOCK_BYTES = 1024 * 1024


class ImportValidationError(ValueError):
    """A user-facing import validation failure."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AudioImportService:
    """Own upload validation, normalization, work claiming, and retry cleanup."""

    def __init__(self, app):
        self.app = app
        self.store = app.extensions["audio_store"]

    @property
    def max_bytes(self) -> int:
        return int(self.app.config["IMPORT_MAX_BYTES"])

    @property
    def max_duration_ms(self) -> int:
        return int(self.app.config["IMPORT_MAX_DURATION_SECONDS"]) * 1000

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        # Keep local AI endpoint failures actionable without exposing a Windows
        # socket error to people reviewing an otherwise usable transcript.
        if exc.__class__.__module__.startswith("httpx") and exc.__class__.__name__ in {
            "ConnectError",
            "ConnectTimeout",
        }:
            return "本机 AI 服务暂不可用，逐字稿已保留。启动 AI 服务后，可点击“重新处理”生成会议总结。"
        text = str(exc or "处理失败").replace("\x00", " ").strip()
        return text[:500] or "处理失败"

    def _probe(self, path: Path) -> dict[str, Any]:
        ffprobe = resolve_ffprobe()
        if not ffprobe:
            raise ImportValidationError("本机缺少 ffprobe，无法验证导入音频")
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=format_name,duration:stream=codec_type,codec_name,duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImportValidationError("音频文件验证失败") from exc
        if proc.returncode:
            raise ImportValidationError("无法解码该音频文件")
        try:
            payload = json.loads((proc.stdout or b"{}").decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ImportValidationError("音频文件验证结果无效") from exc
        streams = payload.get("streams") if isinstance(payload, dict) else []
        streams = streams if isinstance(streams, list) else []
        audio = [row for row in streams if isinstance(row, dict) and row.get("codec_type") == "audio"]
        if not audio:
            raise ImportValidationError("文件中没有可用的音频流")
        if any(isinstance(row, dict) and row.get("codec_type") == "video" for row in streams):
            raise ImportValidationError("仅支持音频文件，不支持包含视频流的文件")
        fmt = payload.get("format") if isinstance(payload, dict) else {}
        values = [
            (fmt or {}).get("duration") if isinstance(fmt, dict) else None,
            audio[0].get("duration"),
        ]
        duration_s = next((float(v) for v in values if v not in {None, "", "N/A"}), 0.0)
        if not math.isfinite(duration_s) or duration_s <= 0:
            raise ImportValidationError("无法识别音频时长")
        duration_ms = int(duration_s * 1000)
        if duration_ms > self.max_duration_ms:
            raise ImportValidationError(f"音频时长超过 {self.max_duration_ms // 3_600_000} 小时限制")
        return {
            "duration_ms": duration_ms,
            "format_name": str((fmt or {}).get("format_name") or ""),
            "codec": str(audio[0].get("codec_name") or ""),
        }

    def create_from_upload(
        self,
        user,
        upload: FileStorage | None,
        *,
        name: str | None,
        meeting_type: str,
        expected_speakers: int,
        project_id: str | None = None,
    ) -> tuple[Meeting, MeetingImportJob]:
        """Stream a browser upload to the meeting directory and enqueue it.

        The meeting is created before disk I/O solely to allocate its opaque
        directory.  Any failed validation rolls that just-created record back.
        """
        if upload is None or not (upload.filename or "").strip():
            raise ImportValidationError("请选择要导入的音频文件")
        # Keep the extension from the submitted basename.  ``secure_filename``
        # drops non-ASCII characters, so applying it first turns a Chinese-only
        # name such as ``会议录音.m4a`` into ``m4a`` and hides the real suffix.
        submitted_name = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        suffix = Path(submitted_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            allowed = "、".join(ext.lstrip(".").upper() for ext in sorted(ALLOWED_SUFFIXES))
            raise ImportValidationError(f"仅支持 {allowed} 音频文件")
        filename = "".join(char for char in submitted_name if char >= " " and char != "\x7f")[:300]
        if not filename or Path(filename).suffix.lower() != suffix:
            safe_stem = secure_filename(Path(submitted_name).stem) or "imported-audio"
            filename = f"{safe_stem}{suffix}"[:300]
        meeting = MeetingService().create(
            user,
            name=name,
            project_id=project_id or None,
            expected_speakers=max(1, min(int(expected_speakers or 4), int(self.app.config["IMPORT_MAX_SPEAKERS"]))),
            meeting_type=(meeting_type or "其他").strip()[:64] or "其他",
        )
        temp_path = self.store.source_dir(meeting.id) / "import.uploading"
        final_path = self.store.source_dir(meeting.id) / f"import{suffix}"
        try:
            digest = hashlib.sha256()
            size = 0
            with temp_path.open("wb") as out:
                while True:
                    block = upload.stream.read(_COPY_BLOCK_BYTES)
                    if not block:
                        break
                    size += len(block)
                    if size > self.max_bytes:
                        raise ImportValidationError(f"音频文件超过 {self.max_bytes // (1024 * 1024 * 1024)} GiB 限制")
                    digest.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if not size:
                raise ImportValidationError("上传的音频文件为空")
            probe = self._probe(temp_path)
            temp_path.replace(final_path)
            meeting.consent_given = True
            # Imported recordings never enter RECORDING.  PROCESSING is the
            # durable hand-off point to the dedicated import worker.
            meeting.status = MeetingStatus.PROCESSING.value
            job = MeetingImportJob(
                meeting_id=meeting.id,
                kind="import",
                original_filename=filename[:300],
                source_path=str(final_path),
                source_mime=(upload.mimetype or "")[:120],
                sha256=digest.hexdigest(),
                size_bytes=size,
                duration_ms=probe["duration_ms"],
                state="QUEUED",
                stage="queued",
                progress=0,
            )
            db.session.add(job)
            db.session.commit()
            self.store.write_json(
                meeting.id,
                "import.json",
                {
                    "original_filename": filename,
                    "source_path": str(final_path),
                    "sha256": digest.hexdigest(),
                    "size_bytes": size,
                    "duration_ms": probe["duration_ms"],
                    "format_name": probe["format_name"],
                    "codec": probe["codec"],
                },
            )
            return meeting, job
        except Exception:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            db.session.delete(meeting)
            db.session.commit()
            raise

    @staticmethod
    def status_payload(job: MeetingImportJob) -> dict[str, Any]:
        return {
            "meeting_id": job.meeting_id,
            "job_id": job.id,
            "state": job.state,
            "stage": job.stage,
            "progress": int(job.progress or 0),
            "error": job.error or "",
            "attempts": int(job.attempts or 0),
            "duration_ms": int(job.duration_ms or 0),
            "size_bytes": int(job.size_bytes or 0),
            "original_filename": job.original_filename,
            "kind": job.kind or "import",
        }

    def enqueue_live_postprocess(self, meeting_id: str) -> MeetingImportJob:
        """Queue a browser-recorded meeting without holding its HTTP request.

        The master WAV is assembled by ``PostMeetingGraph``. Persisting the
        hand-off first makes a long post-meeting pass restart-safe and visible
        in the meeting list immediately.
        """
        meeting = db.session.get(Meeting, meeting_id)
        if not meeting:
            raise ImportValidationError("会议不存在")
        job = MeetingImportJob.query.filter_by(meeting_id=meeting_id).first()
        if job and (job.kind or "import") != "live":
            raise ImportValidationError("该会议已有导入处理任务")
        if job and job.state in {"QUEUED", "RUNNING"}:
            return job
        master = self.store.master_path(meeting_id)
        if not job:
            job = MeetingImportJob(
                meeting_id=meeting_id,
                kind="live",
                original_filename="本地会议录音",
                source_path=str(master),
                source_mime="audio/wav",
            )
            db.session.add(job)
        job.kind = "live"
        job.state, job.stage, job.progress, job.error = "QUEUED", "queued", 0, ""
        job.finished_at = None
        meeting.status = MeetingStatus.PROCESSING.value
        db.session.commit()
        return job

    def retry(self, meeting_id: str) -> MeetingImportJob:
        job = MeetingImportJob.query.filter_by(meeting_id=meeting_id).first()
        if not job:
            raise ImportValidationError("该会议不是导入录音，或导入记录不存在")
        is_live = (job.kind or "import") == "live"
        if not is_live and not Path(job.source_path).exists():
            raise ImportValidationError("原始音频文件已不存在，无法重试")
        if is_live and not self.store.master_path(meeting_id).exists():
            raise ImportValidationError("会议主录音不存在，无法重新处理")
        if job.state == "RUNNING":
            raise ImportValidationError("该音频正在处理中")
        if is_live:
            self._clear_live_derived(meeting_id)
        else:
            self._clear_derived(meeting_id)
        meeting = db.session.get(Meeting, meeting_id)
        if meeting:
            meeting.status = MeetingStatus.PROCESSING.value
        job.state, job.stage, job.progress, job.error = "QUEUED", "queued", 0, ""
        job.finished_at = None
        db.session.commit()
        return job

    def recover_running(self) -> int:
        """Requeue work interrupted by a local worker restart."""
        rows = MeetingImportJob.query.filter_by(state="RUNNING").all()
        for job in rows:
            job.state, job.stage, job.progress = "QUEUED", "queued", 0
            job.error = ""
        if rows:
            db.session.commit()
        return len(rows)

    def claim_next(self) -> MeetingImportJob | None:
        # One local launchd worker is intentionally used.  Avoid a distributed
        # queue dependency while preserving a durable state visible to the UI.
        job = MeetingImportJob.query.filter_by(state="QUEUED").order_by(MeetingImportJob.created_at.asc()).first()
        if not job:
            return None
        job.state = "RUNNING"
        job.stage = "normalizing"
        job.progress = 5
        job.error = ""
        job.attempts = int(job.attempts or 0) + 1
        job.started_at = _utcnow()
        meeting = db.session.get(Meeting, job.meeting_id)
        if meeting:
            meeting.status = MeetingStatus.PROCESSING.value
        db.session.commit()
        return job

    def _set_progress(self, job_id: str, stage: str, progress: int) -> None:
        job = db.session.get(MeetingImportJob, job_id)
        if not job or job.state != "RUNNING":
            return
        job.stage = stage[:48]
        job.progress = max(0, min(100, int(progress)))
        db.session.commit()

    def _normalize(self, job: MeetingImportJob) -> Path:
        source = Path(job.source_path).resolve()
        root = self.store.source_dir(job.meeting_id).resolve()
        if not source.is_file() or root not in source.parents:
            raise RuntimeError("导入源音频不存在或路径无效")
        ffmpeg = resolve_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("本机缺少 ffmpeg，无法标准化导入音频")
        master = self.store.master_path(job.meeting_id)
        temp = master.with_name("meeting_master.importing.wav")
        temp.unlink(missing_ok=True)
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(temp),
                ],
                capture_output=True,
                timeout=int(self.app.config["IMPORT_FFMPEG_TIMEOUT_SECONDS"]),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("音频标准化超时或失败") from exc
        if proc.returncode or not temp.exists() or not temp.stat().st_size:
            detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()[:240]
            raise RuntimeError(f"音频标准化失败{(': ' + detail) if detail else ''}")
        try:
            with wave.open(str(temp), "rb") as wav:
                if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise RuntimeError("标准化后的音频格式不正确")
        except wave.Error as exc:
            raise RuntimeError("标准化后的音频无法读取") from exc
        temp.replace(master)
        return master

    @staticmethod
    def _clear_derived(meeting_id: str) -> None:
        """Remove only artifacts generated from a prior imported run."""
        TranscriptSegment.query.filter_by(meeting_id=meeting_id, source=TranscriptSource.FINAL.value).delete(
            synchronize_session=False
        )
        MeetingSummary.query.filter_by(meeting_id=meeting_id).delete(synchronize_session=False)
        Decision.query.filter_by(meeting_id=meeting_id).delete(synchronize_session=False)
        ActionItem.query.filter_by(meeting_id=meeting_id).delete(synchronize_session=False)
        AudioImportService._clear_knowledge_documents(meeting_id)
        db.session.commit()

    @staticmethod
    def _clear_live_derived(meeting_id: str) -> None:
        """Clear regenerable analysis without touching live/raw transcript rows."""
        MeetingSummary.query.filter_by(meeting_id=meeting_id).delete(synchronize_session=False)
        Decision.query.filter_by(meeting_id=meeting_id).delete(synchronize_session=False)
        ActionItem.query.filter_by(meeting_id=meeting_id).delete(synchronize_session=False)
        AudioImportService._clear_knowledge_documents(meeting_id)
        db.session.commit()

    @staticmethod
    def _clear_knowledge_documents(meeting_id: str) -> None:
        """Bulk deletes bypass ORM cascades, so remove dependent chunks first."""
        document_ids = [
            row.id for row in KnowledgeDocument.query.filter_by(source_meeting_id=meeting_id).all()
        ]
        if document_ids:
            KnowledgeChunk.query.filter(KnowledgeChunk.document_id.in_(document_ids)).delete(
                synchronize_session=False
            )
            KnowledgeDocument.query.filter(KnowledgeDocument.id.in_(document_ids)).delete(
                synchronize_session=False
            )

    def run_next(self) -> bool:
        job = self.claim_next()
        if not job:
            return False
        job_id, meeting_id = job.id, job.meeting_id
        try:
            is_live = (job.kind or "import") == "live"
            if is_live:
                self._clear_live_derived(meeting_id)
                self._set_progress(job_id, "assembling", 8)
                master = self.store.master_path(meeting_id)
            else:
                self._clear_derived(meeting_id)
                self._set_progress(job_id, "normalizing", 15)
                master = self._normalize(job)
                self._set_progress(job_id, "transcribing", 25)

            def on_progress(stage: str, progress: int) -> None:
                self._set_progress(job_id, stage, progress)

            graph = PostMeetingGraph(self.app, self.app.extensions["providers"], self.store, progress=on_progress)
            # Both branches have prepared the canonical WAV.  Passing it explicitly is
            # essential for imported recordings: without it the post-meeting graph
            # attempts to assemble browser capture chunks and replaces the imported
            # audio with its short silent fallback.
            graph.run(meeting_id, master_path=str(master) if master.exists() else None)
            job = db.session.get(MeetingImportJob, job_id)
            if job:
                job.state, job.stage, job.progress = "COMPLETED", "completed", 100
                job.finished_at = _utcnow()
                db.session.commit()
            return True
        except Exception as exc:
            self.app.logger.exception("import processing failed meeting=%s", meeting_id)
            # The failed ASR/DB operation may have aborted the current
            # transaction. Roll it back before recording a durable failure;
            # otherwise a job can remain RUNNING forever with no explanation.
            db.session.rollback()
            job = db.session.get(MeetingImportJob, job_id)
            meeting = db.session.get(Meeting, meeting_id)
            if job:
                job.state, job.stage, job.progress = "FAILED", "failed", min(99, int(job.progress or 0))
                job.error, job.finished_at = self._safe_error(exc), _utcnow()
            if meeting:
                meeting.status = MeetingStatus.FAILED.value
            db.session.commit()
            return True
