"""One Recording Host per meeting. Host transfer gap must be < 2 seconds."""

from __future__ import annotations

from datetime import datetime, timezone

from app.exceptions import HostConflictError, HostTransferGapError
from app.models import Meeting, MeetingClient
from app.web.extensions import db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ms(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


class HostService:
    def __init__(self, max_gap_ms: int = 2000):
        self.max_gap_ms = max_gap_ms

    def current_host(self, meeting: Meeting) -> MeetingClient | None:
        return MeetingClient.query.filter_by(meeting_id=meeting.id, is_host=True).first()

    def claim_host(
        self,
        meeting: Meeting,
        client_id: str,
        user_id: str | None = None,
        user_agent: str = "",
        now: datetime | None = None,
    ) -> MeetingClient:
        now = now or _now()
        existing = self.current_host(meeting)
        if existing and existing.client_id != client_id:
            raise HostConflictError(meeting.id, existing.client_id)
        row = MeetingClient.query.filter_by(meeting_id=meeting.id, client_id=client_id).first()
        if not row:
            row = MeetingClient(
                meeting_id=meeting.id,
                client_id=client_id,
                user_id=user_id,
                user_agent=user_agent,
            )
            db.session.add(row)
        row.is_host = True
        row.user_id = user_id or row.user_id
        row.became_host_at = row.became_host_at or now
        row.last_seen_at = now
        meeting.host_client_id = client_id
        meeting.host_user_id = user_id
        db.session.commit()
        return row

    def transfer(
        self,
        meeting: Meeting,
        to_client_id: str,
        user_id: str | None = None,
        now: datetime | None = None,
        from_left_at: datetime | None = None,
        to_started_at: datetime | None = None,
    ) -> tuple[MeetingClient, int]:
        """Transfer host. Gap is computed from old host leave to new host start.

        When timestamps are omitted, transfer is treated as atomic (gap=0).
        """
        now = now or _now()
        old = self.current_host(meeting)
        leave_at = from_left_at or now
        start_at = to_started_at or now
        gap_ms = int(_ms(start_at) - _ms(leave_at))
        if gap_ms < 0:
            gap_ms = 0
        if gap_ms > self.max_gap_ms:
            raise HostTransferGapError(gap_ms, self.max_gap_ms)

        if old:
            old.is_host = False
            old.left_host_at = leave_at

        new_row = MeetingClient.query.filter_by(meeting_id=meeting.id, client_id=to_client_id).first()
        if not new_row:
            new_row = MeetingClient(
                meeting_id=meeting.id,
                client_id=to_client_id,
                user_id=user_id,
            )
            db.session.add(new_row)
        new_row.is_host = True
        new_row.became_host_at = start_at
        new_row.user_id = user_id or new_row.user_id
        new_row.last_seen_at = now
        meeting.host_client_id = to_client_id
        meeting.host_user_id = user_id
        db.session.commit()

        hosts = MeetingClient.query.filter_by(meeting_id=meeting.id, is_host=True).all()
        if len(hosts) != 1:
            raise HostConflictError(meeting.id, ",".join(h.client_id for h in hosts))
        return new_row, gap_ms
