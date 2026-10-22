"""Wake garble matching, pending name-only, and AI-turn LLM fallback."""

from __future__ import annotations

import httpx
import pytest

from app.graphs.ai_turn import FALLBACK_ANSWER, AITurnGraph
from app.providers.llm import FakeLLMProvider
from app.realtime.echo import AI_SPEAKER, EchoFilter
from app.realtime.wake import WakeDetector
from app.services.meeting import MeetingService
from app.services.transcript import TranscriptService
from app.models import User


def test_garble_wake_is_addressed():
    w = WakeDetector()
    assert w.is_addressed("陈就精灵请回答") is True
    assert w.should_respond("陈就精灵请回答") is True
    assert w.extract_question("陈就精灵请回答") == "请根据刚才的会议内容回答"
    for garble in ("陈酒精灵", "陈酒精", "陈酒金", "程陈经灵", "程陈精灵"):
        assert w.is_addressed(f"{garble}总结一下") is True, garble


def test_chengjiu_alone_does_not_fire_llm_yet():
    w = WakeDetector()
    assert w.should_respond("成就精灵") is False
    assert w.has_pending() is True
    assert w.should_respond("总结说话内容") is True
    assert w.extract_question("总结说话内容") == "总结说话内容"
    assert w.has_pending() is False


def test_extract_question_from_full_wake_utterance():
    w = WakeDetector()
    assert w.extract_question("成就精灵总结说话内容") == "总结说话内容"
    assert w.should_respond("成就精灵总结说话内容") is True


def test_ai_turn_fallback_on_llm_http_error(app):
    class BoomLLM(FakeLLMProvider):
        def stream(self, prompt: str, *, model: str | None = None):
            req = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
            resp = httpx.Response(502, request=req, text="bad gateway")
            raise httpx.HTTPStatusError("502", request=req, response=resp)

    class SilentTTS:
        def synthesize(self, text: str) -> bytes:
            return b"RIFF"

    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="wake-fallback")
        TranscriptService().add_segment(m.id, "我们在讨论延迟", speaker_label="Speaker 1")
        graph = AITurnGraph(BoomLLM(), SilentTTS(), EchoFilter())
        turn = graph.maybe_run(m.id, "成就精灵总结说话内容")
        assert turn is not None
        assert turn["answer"] == FALLBACK_ANSWER
        assert turn["question"] == "总结说话内容"
        segs = TranscriptService().list_for_meeting(m.id)
        ai = [s for s in segs if s.speaker_label == AI_SPEAKER]
        assert len(ai) == 1
        assert ai[0].raw_text == FALLBACK_ANSWER


def test_ai_turn_skips_name_only(app):
    class SilentTTS:
        def synthesize(self, text: str) -> bytes:
            return b""

    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="wake-name-only")
        graph = AITurnGraph(FakeLLMProvider(), SilentTTS(), EchoFilter())
        assert graph.maybe_run(m.id, "成就精灵") is None


def test_ai_answer_uses_generation_start_time_instead_of_commit_time(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        meeting = MeetingService().create(admin, name="ai-timeline")
        graph = AITurnGraph(FakeLLMProvider(), object())
        segment = graph.persist_answer(
            meeting.id,
            "请回答",
            "这是成就精灵的回答。",
            start_ms=12_340,
            end_ms=15_600,
        )
        assert segment.speaker_label == AI_SPEAKER
        assert (segment.start_ms, segment.end_ms) == (12_340, 15_600)


def test_ai_turn_prompt_uses_meeting_as_context_not_a_hard_boundary(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="open-ended-assistant")
        TranscriptService().add_segment(m.id, "我们正在评估两个本地部署方案。", speaker_label="Speaker 1")
        prompt = AITurnGraph(FakeLLMProvider(), object()).build_prompt(m.id, "你认为应该如何选择？")
    assert "不是回答范围的边界" in prompt
    assert "通用知识、逻辑推理和建设性建议" in prompt
    assert "正在参与这场会议的资深同事" in prompt
    assert "不复述用户问题" in prompt
    assert "核心判断" in prompt
    assert "会议背景（仅作参考）" in prompt
    assert "仅根据上下文" not in prompt


def test_new_wake_garbles_are_addressed():
    w = WakeDetector()
    for garble in ("成就金临", "成就金灵", "成酒精灵", "程就精灵", "陈九精灵"):
        assert w.is_addressed(f"{garble}总结一下") is True, garble


def test_correct_wake_span_rewrites_aliases():
    from app.realtime.wake import correct_asr_text, correct_wake_span

    assert correct_wake_span("陈酒精灵请回答") == "成就精灵请回答"
    assert correct_asr_text("程陈经灵总结说话内容") == "成就精灵总结说话内容"
    assert correct_asr_text("成就金临，总结一下") == "成就精灵，总结一下"
    assert correct_asr_text("成酒精灵请说") == "成就精灵请说"


def test_stop_command_accepts_observed_wake_and_stop_garbles():
    from app.realtime.wake import canonical_stop_command

    wake = WakeDetector()
    for observed in (
        "成就精灵停止回复",
        "成久精灵停止回复",
        "城市精灵停止回复",
        "成就有精灵挺止回复的",
    ):
        assert wake.is_stop_command(observed), observed
        assert canonical_stop_command(observed) == "成就精灵停止回复。"
    # A bare control is safe only inside an explicitly active dialogue.
    assert not wake.is_stop_command("停止回复")
    assert wake.is_stop_command("停止回复", dialogue_active=True)


def test_correct_asr_text_keeps_request_and_skips_clean():
    from app.realtime.wake import correct_asr_text

    assert correct_asr_text("我们先采集五组数据") == "我们先采集五组数据"
    assert correct_asr_text("成就精灵总结一下") == "成就精灵总结一下"


def test_correct_asr_text_uses_recent_homophone():
    from app.realtime.wake import correct_asr_text

    recent = ["针尖球心补偿需要重新验证"]
    out = correct_asr_text("陈酒精灵总结一下针尖球心补尝", recent)
    assert out == "成就精灵总结一下针尖球心补偿"


def test_correct_asr_text_llm_polish_mocked():
    from app.realtime.wake import correct_asr_text, should_llm_polish

    class Fake:
        def __init__(self):
            self.prompts = []

        def complete(self, prompt, *, json_mode=False, model=None):
            assert json_mode is False
            self.prompts.append(prompt)
            return "成就精灵总结一下针尖球心补偿"

    raw = "成就精灵总结一下针尖球心补尝还要对照旧算法"
    assert should_llm_polish(raw, raw) is True
    llm = Fake()
    out = correct_asr_text(raw, ["今天继续开会"], llm=llm)
    assert out == "成就精灵总结一下针尖球心补偿"
    assert llm.prompts
    assert "不要添加新信息" in llm.prompts[0]
    assert "json" not in llm.prompts[0].lower()


def test_correct_asr_text_skips_llm_when_too_short():
    from app.realtime.wake import correct_asr_text

    class Boom:
        def complete(self, *a, **k):
            raise AssertionError("llm should not run")

    assert correct_asr_text("陈酒精灵请说", llm=Boom()) == "成就精灵请说"
    assert correct_asr_text("目前来", llm=Boom()) == "目前来"


def test_correct_asr_text_llm_502_keeps_original():
    import httpx
    from app.realtime.wake import correct_asr_text

    class Boom:
        def complete(self, prompt, *, json_mode=False, model=None):
            req = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
            resp = httpx.Response(502, request=req, text="bad gateway")
            raise httpx.HTTPStatusError("502", request=req, response=resp)

    raw = "成就精灵总结一下针尖球心补尝还要对照旧算法"
    assert correct_asr_text(raw, llm=Boom()) == raw


def test_accept_llm_rewrite_guardrails():
    from app.graphs.ai_turn import FALLBACK_ANSWER
    from app.realtime.wake import accept_llm_rewrite

    orig = "成就精灵总结一下针尖球心补尝"
    assert accept_llm_rewrite(orig, "") is None
    assert accept_llm_rewrite(orig, FALLBACK_ANSWER) is None
    assert accept_llm_rewrite(orig, orig + "额外编造了一大段会议里没有的内容并且继续写很长") is None
    assert accept_llm_rewrite(orig, "完全不相关的英文 hello world this is invented") is None
    assert accept_llm_rewrite(orig, "成就精灵总结一下针尖球心补偿") == "成就精灵总结一下针尖球心补偿"


def test_rewrite_does_not_copy_stutter_ni_yi():
    from app.realtime.wake import correct_asr_text

    recent = ["你可以你可以可可以看一以"]
    assert correct_asr_text("你可以看整个会议记录啊", recent) == "你可以看整个会议记录啊"
    assert correct_asr_text("成就精灵请看整个会议记录", recent) == "成就精灵请看整个会议记录"
    assert (
        correct_asr_text("你可以使用tts进行语音回复吗", ["你可以你整个会议记录啊"])
        == "你可以使用tts进行语音回复吗"
    )


def test_rewrite_does_not_strip_qing_to_yi():
    from app.realtime.wake import correct_asr_text

    out = correct_asr_text("成就精灵请看整个会议记录", ["成就精灵以看整个会议记录"])
    assert "请看" in out
    assert "以看" not in out
