#!/usr/bin/env python3
"""Seed admin/member/viewer and a COMPLETED meeting fixture for e2e."""

from __future__ import annotations

from pathlib import Path

from app.config import get_config
from app.models import ActionItem, Decision, Meeting, MeetingSummary, TranscriptSegment
from app.models.enums import MeetingStatus
from app.services.meeting import MeetingService
from app.web.extensions import db
from app.web.factory import create_app, seed_users


def main() -> None:
    app = create_app(get_config())
    with app.app_context():
        db.create_all()
        seed_users()
        from app.models import User

        admin = User.query.filter_by(email="admin@local").first()
        if not Meeting.query.filter_by(name="膝关节项目技术讨论").first():
            m = MeetingService().create(admin, name="膝关节项目技术讨论")
            m.status = MeetingStatus.COMPLETED.value
            m.consent_given = True
            db.session.add(
                TranscriptSegment(
                    meeting_id=m.id,
                    raw_text="这个针尖球心补偿目前看还是需要重新标定。",
                    display_text="这个针尖球心补偿目前看还是需要重新标定。",
                    speaker_label="张工",
                    start_ms=24110,
                    end_ms=24800,
                    source="final",
                )
            )
            db.session.add(MeetingSummary(meeting_id=m.id, summary="决定重新采集5组数据。", risks=["球心漂移"]))
            db.session.add(Decision(meeting_id=m.id, text="重新采集5组数据，并与旧版本算法进行对比。"))
            db.session.add(ActionItem(meeting_id=m.id, description="完成针尖球心补偿验证", owner="张工"))
            db.session.commit()
            print("seeded completed meeting", m.id)
        print("seed complete")


if __name__ == "__main__":
    main()
