"""AI turn: only when wake detector fires. Streams LLM then TTS."""

from __future__ import annotations

import threading
import json
from collections.abc import Iterator

from app.logging import get_logger
from app.realtime.echo import AI_SPEAKER, EchoFilter
from app.realtime.wake import WakeDetector
from app.services.meeting_state import MeetingStateService
from app.services.summary import attributed_transcript
from app.services.transcript import TranscriptService

log = get_logger("ai-turn")

FALLBACK_ANSWER = "刚才没接上，请再说一次"
MAX_CONTEXT_CHARS = 6000
MAX_VIEWS_CONTEXT_CHARS = 2600
MAX_STATE_CONTEXT_CHARS = 1100
MAX_RECENT_TRANSCRIPT_CHARS = 2200


class AITurnGraph:
    def __init__(self, llm, tts, echo: EchoFilter | None = None):
        self.llm = llm
        self.tts = tts
        self.echo = echo or EchoFilter()
        self.wake = WakeDetector(self.echo)
        self.states = MeetingStateService()
        self.transcripts = TranscriptService()

    @staticmethod
    def _truncate_tail(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return "…" + text[-limit:]

    @staticmethod
    def _view_score(view: dict, question: str) -> int:
        chars = {c for c in question if "\u4e00" <= c <= "\u9fff" or c.isalnum()}
        if not chars:
            return 0
        rendered = json.dumps(view, ensure_ascii=False)
        return sum(rendered.count(char) for char in chars)

    def _context_snippet(self, meeting_id: str, question: str = "") -> str:
        state = self.states.current(meeting_id)
        segs = self.transcripts.list_for_meeting(meeting_id)
        # Attribution and filtering live in SummaryService so summarisation and
        # interactive Q&A observe the exact same final human statements.
        lines = attributed_transcript(segs).splitlines()
        # Prefer recent unique-ish statements but never collapse lines across
        # speakers: repeating another attendee can itself be meaningful.
        deduped: list[str] = []
        for line in lines:
            if deduped and (
                line == deduped[-1]
            ):
                continue
            deduped.append(line)
        views = [view for view in (state.speaker_views or []) if isinstance(view, dict)]
        views.sort(key=lambda view: self._view_score(view, question), reverse=True)
        views_text = self._truncate_tail(
            json.dumps(views, ensure_ascii=False, separators=(",", ":")),
            MAX_VIEWS_CONTEXT_CHARS,
        )
        state_text = self._truncate_tail(
            json.dumps(
                {
                    "rolling_summary": state.rolling_summary,
                    "current_topic": state.current_topic,
                    "decisions": state.decisions,
                    "open_questions": state.open_questions,
                    "risks": state.risks,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            MAX_STATE_CONTEXT_CHARS,
        )
        recent = self._truncate_tail("\n".join(deduped), MAX_RECENT_TRANSCRIPT_CHARS)
        context = (
            "参会人观点（证据优先；possible_tendencies 不是确定事实）:\n"
            f"{views_text or '[]'}\n\n会议状态:\n{state_text}\n\n近期带归属的逐字稿:\n{recent or '（暂无）'}"
        )
        return self._truncate_tail(context, MAX_CONTEXT_CHARS)

    def build_prompt(self, meeting_id: str, question: str) -> str:
        """Build one canonical prompt for blocking and streaming answer paths."""
        state = self.states.current(meeting_id)
        context = self._context_snippet(meeting_id, question)
        return (
            "你是成就精灵，正在参与这场会议的资深同事兼智能助手。会议内容是重要的背景和"
            "事实来源之一，但不是回答范围的边界。你可以运用通用知识、逻辑推理和建设性建议"
            "回答用户问题；可以肯定、质疑或提出替代方案。涉及会议时，清楚区分“会议已提到”"
            "和“我的分析/建议”，不得编造会议中未出现的事实。对无法确认的实时事实，要坦诚说明"
            "边界。用户询问某人的观点时，优先引用参会人观点中的证据；明确立场、可能倾向和未表态"
            "必须严格区分。不得把“未知说话人”“身份待确认”或“重叠发言”认定为任何实名参会人。"
            "说话像会议里发言的同事：先给结论，只回应当前问题必要的信息。默认用一两段"
            "自然口语或少量短要点完成；不复述用户问题、不机械重述会议背景、不写“核心判断”"
            "“理由分析”等报告标题，也不要为凑篇幅重复。只有用户明确要求深入分析、方案、"
            "对比、步骤或证据时，才按需要展开。三千 token 是能力上限，不是每次回答的目标长度。"
            "使用自然、清晰、克制的中文，但不要展示内部思维过程。避免“哈哈”“太好了”"
            "等情绪化寒暄、感叹号、拟声词和夸张的安慰/鼓励措辞；语音会按中性会议播报呈现。\n当前议题: "
            f"{state.current_topic}\n"
            f"会议背景（仅作参考）:\n{context}\n\n用户问题: {question}"
        )

    def persist_answer(
        self,
        meeting_id: str,
        question: str,
        answer: str,
        *,
        start_ms: int = 0,
        end_ms: int | None = None,
    ):
        """Persist a completed reply at its generation-start position.

        A local TTS reply can finish long after later participants have spoken.
        The transcript must therefore retain the point at which the assistant
        started answering, not the database commit time at synthesis completion.
        """
        segment = self.transcripts.add_segment(
            meeting_id,
            answer,
            start_ms=max(0, int(start_ms or 0)),
            end_ms=max(int(start_ms or 0), int(end_ms if end_ms is not None else start_ms or 0)),
            speaker_label=AI_SPEAKER,
            speaker_confidence=1.0,
        )
        self.states.apply_patch(meeting_id, {"last_ai_turn": {"question": question, "answer": answer}})
        return segment

    def maybe_run(
        self,
        meeting_id: str,
        latest_text: str,
        *,
        question: str | None = None,
    ) -> dict | None:
        if question is None:
            if not self.wake.should_respond(latest_text):
                return None
            question = self.wake.extract_question(latest_text)
        if not (question or "").strip():
            return None
        prompt = self.build_prompt(meeting_id, question)
        answer = ""
        try:
            tokens: list[str] = []
            for tok in self.llm.stream(prompt):
                tokens.append(tok)
            answer = "".join(tokens).strip()
        except Exception as exc:
            # HTTPStatusError (502) and any other LLM failure: still show a reply in UI.
            log.warning(
                "ai turn llm failed meeting=%s question=%s err=%s",
                meeting_id,
                question[:80],
                exc,
                exc_info=True,
            )
            answer = FALLBACK_ANSWER
        if not answer:
            answer = FALLBACK_ANSWER
        self.persist_answer(meeting_id, question, answer)
        self.echo.on_tts(answer)
        audio = b""
        try:
            try:
                audio = self.tts.synthesize(answer)
            except Exception:
                log.warning("ai turn tts failed meeting=%s", meeting_id, exc_info=True)
                audio = b""
            log.info(
                "tts synth meeting=%s bytes=%s tts=%s",
                meeting_id,
                len(audio or b""),
                type(self.tts).__name__,
            )
            return {"answer": answer, "audio": audio, "question": question}
        finally:
            # Always clear ai_speaking — Timer-only can stick if the worker dies mid-delay.
            delay = 0.4
            if audio and len(audio) > 44:
                delay = max(0.4, (len(audio) - 44) / (16000 * 2) + 0.35)
            threading.Timer(delay, self.echo.on_tts_end).start()

    def stream_text(self, prompt: str) -> Iterator[str]:
        yield from self.llm.stream(prompt)
