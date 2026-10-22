from __future__ import annotations

from app.models import AuditLog
from app.web.extensions import db


class AuditService:
    def record(self, operation: str, user_id: str | None = None, target: str = "", ip: str = "", result: str = "ok", extra: dict | None = None) -> AuditLog:
        row = AuditLog(
            user_id=user_id,
            operation=operation,
            target=target,
            ip=ip,
            result=result,
            extra=extra or {},
        )
        db.session.add(row)
        db.session.commit()
        return row
