"""Core requirement tests (MEETING-*, AI-*, RBAC-*)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.exceptions import HostTransferGapError, IllegalStateTransition, PermissionDenied, RawTextImmutableError
from app.models import SpeakerProfile, TranscriptSegment, User
from app.models.enums import MeetingStatus
from app.providers.llm import FakeLLMProvider
from app.realtime.echo import EchoFilter
from app.realtime.wake import WakeDetector
from app.services.host import HostService
from app.services.knowledge import KnowledgeService
from app.services.meeting import MeetingService, assert_transition
from app.services.meeting_state import MeetingStateService
from app.services.speaker import SpeakerIDService
from app.services.summary import SummaryService, parse_json_payload
from app.services.task import SchedulerController, TaskService
from app.services.transcript import TranscriptService
from app.web.extensions import db


def test_single_host_and_transfer_gap(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="host-test")
        hs = HostService(max_gap_ms=2000)
        hs.claim_host(m, "macbook", user_id=admin.id)
        with pytest.raises(Exception):
            hs.claim_host(m, "iphone", user_id=admin.id)
        now = datetime.now(timezone.utc)
        row, gap = hs.transfer(m, "iphone", user_id=admin.id, now=now, from_left_at=now, to_started_at=now)
        assert row.client_id == "iphone"
        assert gap <= 2000
        hosts = [c for c in m.clients if c.is_host]
        # refresh
        from app.models import MeetingClient
        assert MeetingClient.query.filter_by(meeting_id=m.id, is_host=True).count() == 1
        late = now + timedelta(seconds=3)
        m2 = MeetingService().create(admin, name="host-gap")
        hs.claim_host(m2, "a", user_id=admin.id)
        with pytest.raises(HostTransferGapError):
            hs.transfer(m2, "b", user_id=admin.id, from_left_at=now, to_started_at=late)


def test_raw_text_immutable(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="raw")
        svc = TranscriptService()
        seg = svc.add_segment(m.id, "原始识别")
        original = seg.raw_text
        svc.patch(seg, corrected_text="修正后")
        assert seg.raw_text == original
        with pytest.raises(RawTextImmutableError):
            svc.patch(seg, raw_text="被覆盖")
        assert seg.raw_text == original


def test_low_confidence_speaker_stays_generic(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        profile = SpeakerProfile(user_id=admin.id, display_name="张工", embedding=[1.0] + [0.0] * 63)
        db.session.add(profile)
        db.session.commit()
        svc = SpeakerIDService(high=0.75, mid=0.55)
        low = svc.identify([0.01] * 64, [profile], cluster_index=3)
        assert low["speaker_label"] == "Speaker 3"
        assert low["speaker_user_id"] is None
        high = svc.identify([1.0] + [0.0] * 63, [profile], cluster_index=1)
        assert high["speaker_label"] == "张工"


def test_ai_silent_unless_addressed():
    w = WakeDetector()
    assert w.should_respond("我们现在讨论AI算法。") is False
    assert w.should_respond("这个AI模型比较快。") is False
    assert w.should_respond("某个模型是一个AI模型。") is False
    assert w.should_respond("针尖球心补偿需要重新验证。") is False
    assert w.should_respond("AI，你怎么看这个方案？") is True
    assert w.should_respond("小AI，总结一下刚才的内容。") is True
    assert w.should_respond("成就精灵，总结一下刚才的内容。") is True
    assert w.should_respond("助手，帮我分析一下风险吗") is True


def test_stop_command_matches_chengjiu_jingling():
    w = WakeDetector()
    assert w.is_stop_command("成就精灵，停止") is True
    assert w.is_stop_command("成就精灵停") is True
    assert w.is_stop_command("助手别说") is True
    assert w.is_stop_command("小AI停一下") is True
    assert w.is_stop_command("我们继续讨论") is False


def test_echo_filter_asr_like_tts():
    echo = EchoFilter(threshold=0.72)
    text = "我认为目前主要有两个风险"
    echo.on_tts(text)
    assert echo.is_echo(text) is True
    assert echo.filter_asr(text) is None
    assert echo.filter_asr("完全不相干的一句人话") == "完全不相干的一句人话"
    echo.on_tts_end()
    w = WakeDetector(echo)
    echo.on_tts(text)
    assert w.should_respond("AI，你怎么看？") is False


def test_echo_filter_keeps_earlier_streaming_tts_units_for_the_turn():
    echo = EchoFilter(threshold=0.72)
    echo.begin_turn()
    echo.on_tts("先检查声纹库的名称映射配置。")
    echo.on_tts("然后再收集中文近音词的测试样本。")
    # The microphone can still be playing the first unit when the second is
    # already being synthesized; it must not become a false barge-in.
    assert echo.is_echo("检查声纹库的名称映射配置") is True
    assert echo.is_echo("我不同意这个处理方式") is False


def test_meeting_state_version_monotonic(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="state")
        svc = MeetingStateService()
        s1 = svc.apply_patch(m.id, {"current_topic": "球心补偿"})
        s2 = svc.apply_patch(m.id, {"decisions": [{"id": "d1", "text": "重采数据"}]})
        s3 = svc.apply_patch(m.id, {"decisions": [{"id": "d1", "text": "重采5组数据"}]})
        assert s1.version == 1
        assert s2.version == 2
        assert s3.version == 3
        assert s3.version > s2.version > s1.version
        assert s3.decisions[0]["text"] == "重采5组数据"


def test_status_transitions():
    assert_transition(MeetingStatus.CREATED.value, MeetingStatus.PREPARING.value)
    assert_transition(MeetingStatus.PREPARING.value, MeetingStatus.RECORDING.value)
    assert_transition(MeetingStatus.RECORDING.value, MeetingStatus.STOPPING.value)
    with pytest.raises(IllegalStateTransition):
        assert_transition(MeetingStatus.COMPLETED.value, MeetingStatus.RECORDING.value)
    with pytest.raises(IllegalStateTransition):
        assert_transition(MeetingStatus.CREATED.value, MeetingStatus.COMPLETED.value)


def test_fake_llm_structured_summary():
    llm = FakeLLMProvider()
    raw = llm.complete("请输出会议 JSON summary action_items decisions", json_mode=True)
    payload = parse_json_payload(raw)
    assert "summary" in payload
    assert payload["action_items"]
    assert payload["decisions"]


def test_scheduler_cron(app):
    with app.app_context():
        svc = TaskService()
        task = svc.create(name="日报", prompt="生成日报", cron_expression="0 9 * * *")
        fired = []

        def job(task_id):
            fired.append(task_id)

        ctrl = SchedulerController(svc)
        jid = ctrl.add_cron(task, job)
        assert jid in ctrl.job_ids()
        # Directly execute as the scheduler would
        svc.run_once(task)
        from app.models import TaskRun
        assert TaskRun.query.filter_by(task_id=task.id).count() == 1
        # idempotent execution_key
        svc.run_once(task, fire_time=task.runs[0].started_at)
        ctrl.shutdown()


def test_rbac_viewer_cannot_start(app, auth_viewer):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="rbac")
        MeetingService().set_consent(m, True)
        mid = m.id
    resp = auth_viewer.post(f"/api/meetings/{mid}/start", json={"consent": True, "client_id": "v1"})
    assert resp.status_code == 403


def test_knowledge_search_fake_embed(app):
    with app.app_context():
        svc = KnowledgeService()
        svc.create_document("球心补偿算法说明", "针尖球心补偿需要重新标定并采集5组数据。FusionTrack Atracsys")
        svc.create_document("电源采购", "确认 220伏 电源规格。")
        hits = svc.search("针尖球心补偿")
        assert hits
        assert hits[0]["score"] >= hits[-1]["score"]
        assert "球心" in hits[0]["text"] or hits[0]["score"] > 0
