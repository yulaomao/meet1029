"""Semantic turn-taking policy for the hands-free meeting assistant.

ASR endpointing is deliberately not used as an interruption decision: a final
result may be an acknowledgement (``嗯``/``好的``), a correction command, or a
new question.  This small, deterministic layer makes those paths explicit and
keeps the realtime worker free of ad-hoc keyword checks.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_LEXICAL = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_FILLERS = re.compile(r"^[嗯啊哦诶呃唉哈呵]+$")
_ACKNOWLEDGEMENTS = frozenset(
    {
        "嗯", "嗯嗯", "嗯嗯嗯", "啊", "啊啊", "啊呀", "哦", "哦哦", "诶", "呃", "唉",
        "哈哈", "呵呵", "好的", "好", "对", "是", "行", "可以", "没事", "知道了",
        "收到", "明白", "了解", "嗯好的", "嗯好", "啊好", "好呀", "好啊",
    }
)
_ACK_SUFFIX = re.compile(r"^(?:嗯|啊|哦|诶|呃|唉|哈|呵|好的|好|对|是|行|可以|没事|知道了|收到|明白|了解)+(?:啊|呀|呢|吧|哦)*$")

# These phrases are usable intent signals only in combination with a minimum
# duration.  In particular, particles such as “吗/呢/请/可以” are intentionally
# absent: they occur constantly in ordinary meeting speech.
_INTENT_CUES = (
    "等一下", "等等", "暂停", "先别", "停一下", "先停", "重说", "再说", "换个说法",
    "说慢点", "说快点", "慢一点", "快一点", "不对", "不是", "我想", "我觉得", "我需要",
    "帮我", "请你", "怎么", "为什么", "如何", "什么", "能不能", "可不可以", "能否",
    "是否", "哪个", "多少", "总结一下", "解释一下", "分析一下",
)
_HOLD_ONLY = frozenset({"等一下", "等等", "暂停", "先别", "停一下", "先停"})
_CONTINUE_COMMAND = re.compile(r"^(?:继续(?:说|讲|回答)?|然后呢)$")


@dataclass(frozen=True)
class TurnDecision:
    """One final ASR utterance while the assistant is thinking or speaking."""

    action: str  # ignore | hold | handoff
    compact: str

    @property
    def interrupts(self) -> bool:
        return self.action in {"hold", "handoff"}

    @property
    def starts_reply(self) -> bool:
        return self.action == "handoff"


class DialogueTurnPolicy:
    """Pure, testable semantic guards around VAD/ASR final events."""

    def __init__(
        self,
        *,
        min_barge_chars: int = 6,
        min_barge_ms: int = 700,
        short_command_chars: int = 3,
        short_command_ms: int = 450,
        min_follow_up_chars: int = 4,
        min_follow_up_ms: int = 550,
    ):
        self.min_barge_chars = max(1, int(min_barge_chars))
        self.min_barge_ms = max(0, int(min_barge_ms))
        self.short_command_chars = max(1, int(short_command_chars))
        self.short_command_ms = max(0, int(short_command_ms))
        self.min_follow_up_chars = max(1, int(min_follow_up_chars))
        self.min_follow_up_ms = max(0, int(min_follow_up_ms))

    @staticmethod
    def compact(text: str) -> str:
        return "".join(_LEXICAL.findall(text or ""))

    def is_backchannel(self, text: str) -> bool:
        compact = self.compact(text)
        return (
            not compact
            or compact in _ACKNOWLEDGEMENTS
            or bool(_FILLERS.fullmatch(compact))
            or bool(_ACK_SUFFIX.fullmatch(compact))
        )

    @staticmethod
    def _has_intent(compact: str) -> bool:
        return bool(_CONTINUE_COMMAND.fullmatch(compact)) or any(cue in compact for cue in _INTENT_CUES)

    @staticmethod
    def _is_hold_only(compact: str) -> bool:
        return compact in _HOLD_ONLY

    def _is_short_intent(self, compact: str, duration_ms: int) -> bool:
        if duration_ms < self.short_command_ms:
            return False
        # “继续” is deliberately only two characters.  It is a clear command
        # in an already-active dialogue, whereas treating every two-character
        # phrase as an interruption would make acknowledgement noise brittle.
        if _CONTINUE_COMMAND.fullmatch(compact):
            return True
        return len(compact) >= self.short_command_chars and self._has_intent(compact)

    def should_interrupt(self, text: str, *, duration_ms: int) -> bool:
        """Whether speech should cancel an active LLM/TTS turn."""
        compact = self.compact(text)
        if self.is_backchannel(compact):
            return False
        return (
            (len(compact) >= self.min_barge_chars and duration_ms >= self.min_barge_ms)
            or self._is_short_intent(compact, duration_ms)
        )

    def should_accept_follow_up(
        self,
        text: str,
        *,
        duration_ms: int,
        plain_speech_allowed: bool,
    ) -> bool:
        """Whether a final starts the next unaddressed dialogue turn.

        Immediately after the assistant has finished, a substantive statement
        may naturally be a reply.  Later in the open dialogue window we require
        an intent/question cue, preventing unrelated meeting discussion from
        continually waking the assistant.
        """
        compact = self.compact(text)
        if self.is_backchannel(compact) or self._is_hold_only(compact):
            return False
        concise_intent = self._is_short_intent(compact, duration_ms)
        substantive = len(compact) >= self.min_follow_up_chars and duration_ms >= self.min_follow_up_ms
        return concise_intent or (substantive and (plain_speech_allowed or self._has_intent(compact)))

    def decide_active_turn(self, text: str, *, duration_ms: int) -> TurnDecision:
        """Classify a final heard while Qwen is thinking or TTS is playing."""
        compact = self.compact(text)
        if not self.should_interrupt(compact, duration_ms=duration_ms):
            return TurnDecision("ignore", compact)
        if self.should_accept_follow_up(
            compact,
            duration_ms=duration_ms,
            # A barge-in itself is an explicit attempt to take the floor.
            plain_speech_allowed=True,
        ):
            return TurnDecision("handoff", compact)
        return TurnDecision("hold", compact)
