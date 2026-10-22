"""Enumerations stored as strings so SQLite and PostgreSQL both work."""

from __future__ import annotations

from enum import Enum


class RoleName(str, Enum):
    ADMIN = "Admin"
    MEMBER = "Member"
    VIEWER = "Viewer"


class MeetingStatus(str, Enum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    RECORDING = "RECORDING"
    INTERRUPTED = "INTERRUPTED"
    STOPPING = "STOPPING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


LEGAL_TRANSITIONS: dict[str, set[str]] = {
    MeetingStatus.CREATED.value: {MeetingStatus.PREPARING.value, MeetingStatus.RECORDING.value},
    MeetingStatus.PREPARING.value: {
        MeetingStatus.RECORDING.value,
        MeetingStatus.CREATED.value,
        MeetingStatus.FAILED.value,
    },
    MeetingStatus.RECORDING.value: {
        MeetingStatus.INTERRUPTED.value,
        MeetingStatus.STOPPING.value,
    },
    MeetingStatus.INTERRUPTED.value: {
        MeetingStatus.RECORDING.value,
        MeetingStatus.STOPPING.value,
        MeetingStatus.FAILED.value,
    },
    MeetingStatus.STOPPING.value: {
        MeetingStatus.UPLOADING.value,
        MeetingStatus.PROCESSING.value,
        MeetingStatus.FAILED.value,
    },
    MeetingStatus.UPLOADING.value: {
        MeetingStatus.PROCESSING.value,
        MeetingStatus.FAILED.value,
        MeetingStatus.UPLOADING.value,
    },
    MeetingStatus.PROCESSING.value: {
        MeetingStatus.COMPLETED.value,
        MeetingStatus.FAILED.value,
    },
    MeetingStatus.COMPLETED.value: {MeetingStatus.PROCESSING.value},
    MeetingStatus.FAILED.value: {MeetingStatus.PROCESSING.value, MeetingStatus.UPLOADING.value},
}


class TranscriptSource(str, Enum):
    REALTIME = "realtime"
    FINAL = "final"


class TaskRunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    CANCELLED = "CANCELLED"


class ActionItemStatus(str, Enum):
    OPEN = "未开始"
    IN_PROGRESS = "进行中"
    DONE = "完成"
    BLOCKED = "阻塞"


class ScheduleType(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    CRON = "cron"
