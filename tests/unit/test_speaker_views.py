"""Evidence-first per-speaker meeting views."""

from __future__ import annotations

from app.graphs.ai_turn import AITurnGraph
from app.models import User
from app.providers.llm import FakeLLMProvider
from app.services.meeting import MeetingService
from app.services.meeting_state import MeetingStateService
from app.services.summary import SummaryService, attributed_transcript, validate_speaker_views
from app.services.transcript import TranscriptService


def _meeting_with_two_speakers(app):
    admin = User.query.filter_by(email="admin@local").first()
    meeting = MeetingService().create(admin, name="观点归属测试")
    service = TranscriptService()
    alice = service.add_segment(
        meeting.id,
        "我明确支持先做小范围试点。",
        speaker_label="张工",
        speaker_user_id=admin.id,
        speaker_confidence=0.91,
        start_ms=1000,
    )
    bob = service.add_segment(
        meeting.id,
        "我反对直接上线，担心样本量不够。",
        speaker_label="Speaker 2",
        speaker_cluster_id="cluster-b",
        speaker_confidence=0.82,
        start_ms=3000,
    )
    return meeting, alice, bob


def test_summary_saves_evidence_backed_views_and_live_state(app):
    with app.app_context():
        meeting, alice, bob = _meeting_with_two_speakers(app)
        payload = {
            "title": "试点上线方案讨论",
            "summary": "围绕试点上线进行了讨论。",
            "topics": ["上线策略"],
            "decisions": [], "action_items": [], "risks": [], "open_questions": [], "follow_ups": [],
            "speaker_views": [
                {
                    "speaker_key": f"user:{alice.speaker_user_id}", "topic": "上线策略",
                    "explicit_stances": [{"position": "支持", "text": "支持先做小范围试点", "source_segment_ids": [alice.id]}],
                    "key_points": [], "concerns": [], "open_questions": [], "possible_tendencies": [],
                },
                {
                    "speaker_key": "cluster:cluster-b", "topic": "上线策略",
                    "explicit_stances": [{"position": "反对", "text": "反对直接上线", "source_segment_ids": [bob.id]}],
                    "key_points": [], "concerns": [{"text": "样本量不够", "source_segment_ids": [bob.id]}],
                    "open_questions": [], "possible_tendencies": [],
                },
            ],
        }
        summary = SummaryService(FakeLLMProvider(payload)).analyze(meeting)
        views = summary.payload["speaker_views"]
        assert len(views) == 2
        assert views[0]["speaker_label"] == "张工"
        assert views[1]["speaker_label"] == "说话人 2"
        assert views[1]["explicit_stances"][0]["source_segment_ids"] == [bob.id]
        assert MeetingStateService().current(meeting.id).speaker_views == views


def test_view_validation_rejects_cross_speaker_evidence_and_unverified_renaming(app):
    with app.app_context():
        meeting, alice, bob = _meeting_with_two_speakers(app)
        invalid = {
            "speaker_views": [{
                "speaker_key": f"user:{alice.speaker_user_id}",
                "speaker_label": "错误的实名", "topic": "上线策略",
                "explicit_stances": [{"position": "支持", "text": "不应串人", "source_segment_ids": [bob.id]}],
                "key_points": [], "concerns": [], "open_questions": [], "possible_tendencies": [],
            }]
        }
        segments = TranscriptService().list_for_meeting(meeting.id)
        assert validate_speaker_views(invalid, segments) == []
        transcript = attributed_transcript(segments)
        assert "speaker_key:user:" in transcript
        assert "Speaker 2" not in transcript  # generic label is normalized for safe presentation
        assert "说话人 2" in transcript


def test_malformed_view_does_not_erase_prior_valid_live_view(app):
    with app.app_context():
        meeting, alice, bob = _meeting_with_two_speakers(app)
        previous = [{"id": "prior", "speaker_key": f"user:{alice.speaker_user_id}", "speaker_label": "张工", "topic": "上线策略"}]
        MeetingStateService().apply_patch(meeting.id, {"speaker_views": previous}, replace_fields={"speaker_views"})
        payload = {
            "summary": "模型返回了无效归属。", "topics": [], "decisions": [], "action_items": [],
            "risks": [], "open_questions": [], "follow_ups": [],
            "speaker_views": [{
                "speaker_key": f"user:{alice.speaker_user_id}", "topic": "上线策略",
                "explicit_stances": [{"position": "支持", "text": "错误引用", "source_segment_ids": [bob.id]}],
            }],
        }
        summary = SummaryService(FakeLLMProvider(payload)).analyze(meeting)
        assert summary.payload["speaker_views"] == previous
        assert MeetingStateService().current(meeting.id).speaker_views == previous


def test_ai_prompt_keeps_speaker_attribution_and_filters_ai_segments(app):
    with app.app_context():
        meeting, alice, _bob = _meeting_with_two_speakers(app)
        TranscriptService().add_segment(meeting.id, "这是 AI 自己的回答", speaker_label="成就精灵")
        MeetingStateService().apply_patch(meeting.id, {
            "speaker_views": [{
                "id": "view-1", "speaker_key": f"user:{alice.speaker_user_id}", "speaker_label": "张工",
                "topic": "上线策略", "explicit_stances": [{"position": "支持", "text": "支持试点", "source_segment_ids": [alice.id]}],
            }]
        }, replace_fields={"speaker_views"})
        prompt = AITurnGraph(FakeLLMProvider(), object()).build_prompt(meeting.id, "谁支持试点？")
        assert "参会人观点（证据优先" in prompt
        assert "张工" in prompt
        assert "这是 AI 自己的回答" not in prompt
        assert "可能倾向和未表态" in prompt


def test_final_summary_includes_ai_narrative_but_normalizes_task_actions(app):
    with app.app_context():
        meeting, alice, _bob = _meeting_with_two_speakers(app)
        ai = TranscriptService().add_segment(
            meeting.id, "我建议先用小范围试点验证效果。", speaker_label="成就精灵", start_ms=5000
        )
        payload = {
            "title": "试点验证讨论", "summary": "参会人讨论先试点，AI给出验证建议。",
            "topics": ["试点"], "decisions": [{"content": "先做小范围试点", "source_segment_id": alice.id}],
            "action_items": [{"task": "验证试点效果", "owner": "张工", "source_segment_id": ai.id}],
            "risks": [], "open_questions": [], "follow_ups": [], "speaker_views": [],
        }
        summary = SummaryService(FakeLLMProvider(payload)).analyze(meeting)
        assert "AI给出验证建议" in summary.summary
        assert summary.payload["decisions"][0]["text"] == "先做小范围试点"
        assert summary.payload["action_items"][0]["description"] == "验证试点效果"


def test_attribution_uses_corrected_final_text_not_raw_asr(app):
    with app.app_context():
        meeting, alice, _bob = _meeting_with_two_speakers(app)
        TranscriptService().apply_correction(alice, "我明确支持先做小范围试点，再评估结果。")
        text = attributed_transcript(TranscriptService().list_for_meeting(meeting.id))
        assert "再评估结果" in text
        assert "我明确支持先做小范围试点。" not in text


def test_explicit_meeting_query_uses_canonical_context(app, auth_admin):
    with app.app_context():
        meeting, _alice, _bob = _meeting_with_two_speakers(app)
        meeting_id = meeting.id
        provider = app.extensions["providers"].llm
        provider.calls.clear()
    response = auth_admin.post("/api/ai/query", json={"meeting_id": meeting_id, "query": "谁持反对意见？"})
    assert response.status_code == 200
    assert provider.calls
    assert "近期带归属的逐字稿" in provider.calls[-1]
    assert "说话人 2" in provider.calls[-1]


def test_detail_renders_view_and_source_link(app, auth_admin):
    with app.app_context():
        meeting, alice, _bob = _meeting_with_two_speakers(app)
        payload = {
            "summary": "已生成观点。", "topics": [], "decisions": [], "action_items": [],
            "risks": [], "open_questions": [], "follow_ups": [],
            "speaker_views": [{
                "speaker_key": f"user:{alice.speaker_user_id}", "topic": "上线策略",
                "explicit_stances": [{"position": "支持", "text": "支持先做试点", "source_segment_ids": [alice.id]}],
            }],
        }
        SummaryService(FakeLLMProvider(payload)).analyze(meeting)
        meeting_id = meeting.id
        alice_id = alice.id
    response = auth_admin.get(f"/meetings/{meeting_id}")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "参会人观点" in page
    assert "支持先做试点" in page
    assert f'href="#seg-{alice_id}"' in page
