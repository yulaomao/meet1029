"""System health: web, db, providers, heartbeat freshness."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from app.models import Meeting
from app.models.enums import MeetingStatus
from app.web.extensions import db


HEARTBEAT_DEGRADED_S = 15


class HealthService:
    def __init__(self, providers=None):
        self.providers = providers

    def check(self) -> dict:
        db_ok = True
        db_error = ""
        try:
            db.session.execute(text("SELECT 1"))
        except Exception as exc:
            db_ok = False
            db_error = str(exc)

        live = Meeting.query.filter(Meeting.status == MeetingStatus.RECORDING.value).all()
        degraded = []
        now = datetime.now(timezone.utc)
        for m in live:
            hb = m.last_heartbeat_at
            if hb is None:
                degraded.append({"meeting_id": m.id, "reason": "missing heartbeat"})
                continue
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            if (now - hb).total_seconds() > HEARTBEAT_DEGRADED_S:
                degraded.append({"meeting_id": m.id, "reason": "heartbeat stale"})

        overall = "ok"
        if not db_ok or degraded:
            overall = "degraded"
        speech = "ok"
        llm = "ok"
        if self.providers:
            speech = type(self.providers.asr).__name__
            llm = type(self.providers.llm).__name__
        return {
            "status": overall,
            "web": "ok",
            "database": "ok" if db_ok else "error",
            "database_error": db_error,
            "asr": speech,
            "sensevoice": type(self.providers.sensevoice).__name__ if self.providers and self.providers.sensevoice else "disabled",
            "llm": llm,
            "tts": type(self.providers.tts).__name__ if self.providers else "unknown",
            "live_meetings": len(live),
            "degraded_meetings": degraded,
        }
