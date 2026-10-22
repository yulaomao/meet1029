"""Meeting CRUD, consent gate, status transitions."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.exceptions import ConsentRequired, IllegalStateTransition, PermissionDenied
from app.models import Meeting, MeetingParticipant
from app.models.enums import LEGAL_TRANSITIONS, MeetingStatus
from app.security.rbac import can, require
from app.web.extensions import db


SHANGHAI = ZoneInfo("Asia/Shanghai")
_AUTO_MEETING_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+临时会议$")
_TITLE_CLEANUP = re.compile(r"[\s，。！？；：、,.!?;:【】\[\]#*_`]+")


def default_meeting_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    return stamp.strftime("%Y-%m-%d %H:%M") + " 临时会议"


def is_auto_meeting_name(name: str | None) -> bool:
    """True only for the timestamp placeholder produced by this application."""
    return bool(_AUTO_MEETING_NAME.fullmatch((name or "").strip()))


def assert_transition(current: str, target: str) -> None:
    allowed = LEGAL_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise IllegalStateTransition(current, target)


class MeetingService:
    def create(
        self,
        user,
        name: str | None = None,
        project_id: str | None = None,
        expected_speakers: int = 4,
        meeting_type: str = "其他",
        participants: list[str] | None = None,
    ) -> Meeting:
        require(user, "create_meeting")
        title = (name or "").strip() or default_meeting_name()
        meeting = Meeting(
            name=title,
            project_id=project_id,
            created_by=user.id,
            status=MeetingStatus.CREATED.value,
            expected_speakers=expected_speakers,
            meeting_type=meeting_type,
        )
        db.session.add(meeting)
        db.session.flush()
        for display in participants or []:
            db.session.add(
                MeetingParticipant(meeting_id=meeting.id, display_name=display)
            )
        db.session.commit()
        return meeting

    def get(self, meeting_id: str) -> Meeting | None:
        return db.session.get(Meeting, meeting_id)

    def list_meetings(self, limit: int = 50) -> list[Meeting]:
        return (
            Meeting.query.order_by(Meeting.created_at.desc()).limit(limit).all()
        )

    def prepare(self, meeting: Meeting) -> Meeting:
        if meeting.status == MeetingStatus.CREATED.value:
            assert_transition(meeting.status, MeetingStatus.PREPARING.value)
            meeting.status = MeetingStatus.PREPARING.value
            db.session.commit()
        return meeting

    def set_consent(self, meeting: Meeting, consented: bool) -> Meeting:
        meeting.consent_given = bool(consented)
        db.session.commit()
        return meeting

    def start(self, meeting: Meeting, user, client_id: str | None = None) -> Meeting:
        if not can(user, "start_meeting"):
            raise PermissionDenied("start_meeting", getattr(user, "role", ""))
        if not meeting.consent_given:
            raise ConsentRequired()
        target = MeetingStatus.RECORDING.value
        if meeting.status not in {MeetingStatus.RECORDING.value}:
            assert_transition(meeting.status, target)
            meeting.status = target
        if meeting.started_at is None:
            meeting.started_at = datetime.now(SHANGHAI)
        if client_id and not meeting.host_client_id:
            meeting.host_client_id = client_id
            meeting.host_user_id = user.id
        db.session.commit()
        return meeting

    def transition(self, meeting: Meeting, target: str) -> Meeting:
        assert_transition(meeting.status, target)
        meeting.status = target
        if target in {MeetingStatus.STOPPING.value, MeetingStatus.COMPLETED.value}:
            meeting.ended_at = meeting.ended_at or datetime.now(SHANGHAI)
        db.session.commit()
        return meeting

    def stop(self, meeting: Meeting, *, remaining_chunks: int = 0, force: bool = False) -> Meeting:
        if meeting.status == MeetingStatus.RECORDING.value:
            self.transition(meeting, MeetingStatus.STOPPING.value)
        if remaining_chunks > 0 and not force:
            if meeting.status == MeetingStatus.STOPPING.value:
                self.transition(meeting, MeetingStatus.UPLOADING.value)
            return meeting
        if meeting.status in {MeetingStatus.STOPPING.value, MeetingStatus.UPLOADING.value}:
            if meeting.status == MeetingStatus.STOPPING.value:
                # skip uploading if nothing remaining
                assert_transition(meeting.status, MeetingStatus.PROCESSING.value)
                meeting.status = MeetingStatus.PROCESSING.value
                db.session.commit()
            elif meeting.status == MeetingStatus.UPLOADING.value:
                self.transition(meeting, MeetingStatus.PROCESSING.value)
        return meeting

    def mark_completed(self, meeting: Meeting) -> Meeting:
        if meeting.status != MeetingStatus.COMPLETED.value:
            self.transition(meeting, MeetingStatus.COMPLETED.value)
        return meeting

    def assign_auto_title(self, meeting: Meeting, payload: dict | None = None) -> Meeting:
        """Replace an untouched timestamp placeholder after the meeting is analysed.

        A user-provided title is never changed.  The summary prompt asks Qwen for
        a title; topics and the first substantive transcript sentence provide a
        deterministic local fallback when that field is absent or malformed.
        """
        if not meeting or not is_auto_meeting_name(meeting.name):
            return meeting
        payload = payload if isinstance(payload, dict) else {}
        candidates: list[str] = []
        title = payload.get("title")
        if isinstance(title, str):
            candidates.append(title)
        topics = payload.get("topics") or []
        if isinstance(topics, list):
            candidates.extend(str(topic) for topic in topics if isinstance(topic, str))
        for candidate in candidates:
            clean = _TITLE_CLEANUP.sub("", candidate or "").strip()
            if len(clean) < 4:
                continue
            clean = clean[:32]
            if not clean.endswith(("会", "会议", "讨论", "评审", "沟通")):
                clean += "讨论"
            meeting.name = clean[:300]
            db.session.commit()
            return meeting
        # The fallback remains grounded in the actual meeting, even if the
        # summarizer timed out or returned non-JSON.
        from app.models import TranscriptSegment

        seg = (
            TranscriptSegment.query.filter_by(meeting_id=meeting.id)
            .order_by(TranscriptSegment.start_ms.asc())
            .first()
        )
        source = (seg.display_text or seg.corrected_text or seg.raw_text) if seg else ""
        clean = _TITLE_CLEANUP.sub("", source or "").strip()[:24]
        meeting.name = (clean + "讨论") if len(clean) >= 4 else "会议讨论"
        db.session.commit()
        return meeting

    def mark_failed(self, meeting: Meeting) -> Meeting:
        if meeting.status != MeetingStatus.FAILED.value:
            self.transition(meeting, MeetingStatus.FAILED.value)
        return meeting

    def heartbeat(self, meeting: Meeting) -> Meeting:
        meeting.last_heartbeat_at = datetime.now(SHANGHAI)
        db.session.commit()
        return meeting
