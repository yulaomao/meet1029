"""Keyword wake detection on latest transcript window.

AI is SILENT unless explicitly addressed (成就精灵 / AI / 小AI / 助手). The remainder is the request.
No wake-word acoustic model. Cooldown while AI is speaking.

Live FunASR may garble 成就精灵 into near-homophones; match those aliases and a
fuzzy 3–4 character window as well. Transcript correction itself is performed
later by the batched, constrained Qwen editor in the speech pipeline.
"""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher

from app.logging import get_logger
from app.realtime.echo import EchoFilter

log = get_logger("wake")

CANONICAL_WAKE = "成就精灵"
# Exact / near-homophone spans observed in live Chinese ASR output.
WAKE_ALIASES = (
    "成就精灵",
    "陈就精灵",
    "陈酒精灵",
    "程陈经灵",
    "程陈精灵",
    "成就金临",
    "成就金灵",
    "成就精玲",
    "成就晶灵",
    "成就经灵",
    "成酒精灵",
    "程酒精灵",
    "程就精灵",
    "陈就金灵",
    "陈就经灵",
    "陈九精灵",
    "成旧精灵",
    "承就精灵",
    "陈酒精",
    "陈酒金",
    # Near-homophones observed in short live meetings
    "神巧精灵",
    "神巧精玲",
    "神巧金灵",
    "成巧精灵",
)


def _alias_alternation() -> str:
    aliases = sorted(set(WAKE_ALIASES), key=len, reverse=True)
    return "|".join(re.escape(a) for a in aliases)


WAKE_PATTERN = re.compile(
    r"(" + _alias_alternation() + r"|小?\s*AI|助手)",
    re.IGNORECASE,
)
QUESTION_HINTS = re.compile(
    r"(吗|呢|什么|怎么|如何|为什么|能否|可以|请|帮|总结|分析|看一下|\?|？)"
)
# Stop must be more tolerant than a normal wake: an ASR error here otherwise
# turns “成久精灵停止回复” into a fresh (and unwanted) assistant question.
# The broad part is intentionally coupled to an explicit stop action, so an
# ordinary mention of a similarly named person cannot terminate a dialogue.
_STOP_WAKE = r"(?:" + _alias_alternation() + r"|小?\s*AI|助手|[成城程陈橙].{0,3}精[灵玲])"
_STOP_ACTION = r"(?:停(?:止)?|挺止|听止|别(?:说|回复|回答)(?:了)?|结束(?:回复|对话)?)"
STOP_PATTERN = re.compile(_STOP_WAKE + r".{0,8}" + _STOP_ACTION, re.IGNORECASE)
_BARE_STOP_PATTERN = re.compile(
    r"^(?:请)?(?:停(?:止)?|挺止|听止)(?:回复|回答|说话|播报)?(?:一下|了)?$"
    r"|^(?:请)?别(?:说|回复|回答)(?:了)?$|^(?:请)?结束(?:回复|对话)$",
    re.IGNORECASE,
)
# Mentions of "AI" as a topic, not a call-out.
NEGATIVE = re.compile(
    r"(这个AI|讨论AI|AI模型|AI算法|某个模型是一个AI|我们.*AI)",
    re.IGNORECASE,
)
_ADDRESS_START = re.compile(
    r"^(" + _alias_alternation() + r"|小?\s*AI|助手)[，,：:\s]",
    re.IGNORECASE,
)
_BARE_ANSWER = frozenset({"请回答", "回答", "请说"})
_BARE_REQUESTS = frozenset(
    {
        "请回答",
        "回答",
        "请说",
        "总结一下",
        "总结",
        "总结说话内容",
        "请根据刚才的会议内容回答",
    }
)
_DEFAULT_ANSWER = "请根据刚才的会议内容回答"
_FUZZY_MIN_RATIO = 0.55
_PENDING_WINDOW_S = 15.0
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WAKE_GARBLE_RE = re.compile(
    "|".join(
        re.escape(a)
        for a in sorted((x for x in set(WAKE_ALIASES) if x != CANONICAL_WAKE), key=len, reverse=True)
    )
)
_REJECT_LLM = "刚才没接上，请再说一次"
_LLM_PROMPT = (
    "会议上文:\n{context}\n\n"
    "这一句ASR:\n{utterance}\n\n"
    "请只修正口误/同音错字，不要添加新信息。只输出修正后的那一句。"
)


def _prefix_edit_distance(a: str, b: str) -> int:
    """Hamming distance when same length; otherwise large."""
    if len(a) != len(b):
        return 99
    return sum(x != y for x, y in zip(a, b))


def fuzzy_wake_span(text: str) -> tuple[int, int] | None:
    t = text or ""
    if not t:
        return None
    best: tuple[float, int, int] | None = None
    # Special-case: 4-char window ending in 精灵 (or containing it) with ≤2 edits vs 成就精灵.
    if len(t) >= 4:
        for i in range(0, len(t) - 3):
            window = t[i : i + 4]
            if "精灵" not in window and not window.endswith("精玲"):
                continue
            dist = _prefix_edit_distance(window, CANONICAL_WAKE)
            if dist <= 2:
                # Prefer exact-ish matches; treat as ratio 1.0 - 0.1*dist
                score = 1.0 - 0.1 * dist
                if best is None or score > best[0]:
                    best = (score, i, i + 4)
    for size in (4, 3):
        if len(t) < size:
            continue
        for i in range(0, len(t) - size + 1):
            window = t[i : i + size]
            ratio = SequenceMatcher(None, window, CANONICAL_WAKE).ratio()
            if ratio < _FUZZY_MIN_RATIO:
                continue
            if best is None or ratio > best[0]:
                best = (ratio, i, i + size)
    if best is None:
        return None
    return best[1], best[2]


def correct_wake_span(text: str) -> str:
    """Replace known / fuzzy 成就精灵 garbles; keep the rest of the utterance."""
    t = text or ""
    if not t:
        return t
    if _WAKE_GARBLE_RE.pattern:
        t = _WAKE_GARBLE_RE.sub(CANONICAL_WAKE, t)
    if CANONICAL_WAKE in t:
        return t
    span = fuzzy_wake_span(t)
    if span is None:
        return t
    start, end = span
    window = t[start:end]
    if window in {"助手"} or re.fullmatch(r"小?\s*AI", window, re.IGNORECASE):
        return t
    return t[:start] + CANONICAL_WAKE + t[end:]


def canonical_stop_command(text: str, *, dialogue_active: bool = False) -> str | None:
    """Return a canonical stop sentence only for an unambiguous command.

    This is a routing/display aid. It never changes the stored raw ASR text;
    callers retain that immutable evidence alongside the normalized text.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    normalized = correct_wake_span(raw)
    if STOP_PATTERN.search(raw) or STOP_PATTERN.search(normalized):
        return f"{CANONICAL_WAKE}停止回复。"
    compact = re.sub(r"[\s，。！？；：、,.!?;:]+", "", raw)
    if dialogue_active and _BARE_STOP_PATTERN.fullmatch(compact):
        return f"{CANONICAL_WAKE}停止回复。"
    return None


def _span_overlaps_canonical_wake(text: str, start: int, n: int) -> bool:
    idx = text.find(CANONICAL_WAKE)
    if idx < 0:
        return False
    return not (start + n <= idx or start >= idx + len(CANONICAL_WAKE))


def _hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        return 99
    return sum(x != y for x, y in zip(a, b))


_FUNCTION_SWAP = set("的了是在有和就都而及与或但请看以你我他她它这那吗呢吧啊呀么能")


def _has_consecutive_cjk_dup(s: str) -> bool:
    prev = ""
    for ch in s or "":
        if ch == prev and "\u4e00" <= ch <= "\u9fff":
            return True
        prev = ch
    return False


def _single_char_diff(a: str, b: str) -> tuple[str, str] | None:
    if len(a) != len(b):
        return None
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    if len(diffs) != 1:
        return None
    return diffs[0]


def rewrite_from_recent_context(text: str, recent_lines: list[str] | None) -> str:
    """If a window is one-char-off a recent caption term, adopt the recent spelling.

    Do not copy stutter/garble from a previous line (你你 / 请→以 / 看→你).
    """
    t = text or ""
    if not t or not recent_lines:
        return t
    recent = [ln for ln in recent_lines if ln and not _has_consecutive_cjk_dup(ln)]
    if not recent:
        return t
    candidates: list[tuple[int, int, str, float]] = []
    for n in (6, 5, 4):
        if len(t) < n:
            continue
        for i in range(0, len(t) - n + 1):
            window = t[i : i + n]
            if not _CJK_RUN.fullmatch(window):
                continue
            if window == CANONICAL_WAKE or window in WAKE_ALIASES:
                continue
            if _span_overlaps_canonical_wake(t, i, n):
                continue
            best_term: str | None = None
            best_ratio = 0.0
            exact = False
            for line in recent:
                if len(line) < n:
                    continue
                for j in range(0, len(line) - n + 1):
                    term = line[j : j + n]
                    if not _CJK_RUN.fullmatch(term):
                        continue
                    if term == CANONICAL_WAKE or term in WAKE_ALIASES:
                        continue
                    if term == window:
                        exact = True
                        break
                    if _hamming(window, term) != 1:
                        continue
                    ratio = SequenceMatcher(None, window, term).ratio()
                    if ratio >= 0.75 and ratio > best_ratio:
                        best_ratio = ratio
                        best_term = term
                if exact:
                    break
            if exact or best_term is None:
                continue
            pair = _single_char_diff(window, best_term)
            if pair and (pair[0] in _FUNCTION_SWAP or pair[1] in _FUNCTION_SWAP):
                continue
            candidates.append((i, n, best_term, best_ratio))
    if not candidates:
        return t
    candidates.sort(key=lambda c: (-c[3], -c[1], c[0]))
    used: list[tuple[int, int]] = []
    out = t
    for i, n, term, _ratio in candidates:
        if any(not (i + n <= a or i >= b) for a, b in used):
            continue
        if out[i : i + n] != t[i : i + n]:
            continue
        proposed = out[:i] + term + out[i + n :]
        if _has_consecutive_cjk_dup(proposed) and not _has_consecutive_cjk_dup(out):
            continue
        out = proposed
        used.append((i, i + n))
    return out


def should_llm_polish(original: str, local: str) -> bool:
    """Skip 27B when the line is already clean or too short."""
    local_t = (local or "").strip()
    orig_t = (original or "").strip()
    if len(local_t) < 8:
        return False
    remainder = local_t.replace(CANONICAL_WAKE, "").strip("，,：: 。.！!？? ")
    if not remainder or remainder in _BARE_ANSWER or remainder in _BARE_REQUESTS:
        return False
    if len(remainder) < 6:
        return False
    changed = local_t != orig_t
    addressed = CANONICAL_WAKE in local_t or CANONICAL_WAKE in orig_t
    if not changed and not addressed:
        return False
    return True


def accept_llm_rewrite(original: str, proposed: str) -> str | None:
    orig = (original or "").strip()
    out = (proposed or "").strip()
    if not out or not orig:
        return None
    out = out.splitlines()[0].strip().strip("\"'`")
    if not out or out.startswith("```"):
        return None
    if out == _REJECT_LLM or _REJECT_LLM in out:
        return None
    n, m = len(out), len(orig)
    if n < max(1, int(m * 0.5)) or n > int(m * 1.8) + 1:
        return None
    if SequenceMatcher(None, orig, out).ratio() < 0.4:
        return None
    return out


def polish_asr_with_llm(text: str, recent_lines: list[str], llm) -> str:
    clipped: list[str] = []
    total = 0
    for ln in (recent_lines or [])[-8:]:
        s = (ln or "").strip()
        if not s:
            continue
        if len(s) > 40:
            s = s[:40]
        clipped.append(s)
        total += len(s)
        if total >= 200:
            break
    context = "\n".join(clipped) if clipped else "（无）"
    prompt = _LLM_PROMPT.format(context=context, utterance=(text or "").strip())
    return (llm.complete(prompt, json_mode=False) or "").strip()


def correct_asr_text(
    raw: str,
    recent_lines: list[str] | None = None,
    *,
    llm=None,
    force_llm: bool = False,
) -> str:
    """Cheap local rewrite first; optional LLM polish (never invents content)."""
    raw_t = raw or ""
    if not raw_t.strip():
        return raw_t
    local = correct_wake_span(raw_t)
    local = rewrite_from_recent_context(local, recent_lines)
    if llm is None:
        return local
    if not force_llm and not should_llm_polish(raw_t, local):
        return local
    try:
        out = polish_asr_with_llm(local, list(recent_lines or []), llm)
    except Exception:
        log.warning("asr llm polish failed", exc_info=True)
        return local
    accepted = accept_llm_rewrite(local, out)
    return accepted or local


class WakeDetector:
    def __init__(self, echo: EchoFilter | None = None, cooldown_s: float = 2.0):
        self.echo = echo or EchoFilter()
        self.cooldown_s = cooldown_s
        self._last_fire_mono: float = 0.0
        self._pending: bool = False
        self._pending_at: float = 0.0

    def has_pending(self) -> bool:
        return bool(self._pending)

    def looks_like_question(self, text: str) -> bool:
        t = text or ""
        return bool(QUESTION_HINTS.search(t) or t.strip().endswith(("?", "？")))

    def _fuzzy_wake_span(self, text: str) -> tuple[int, int] | None:
        return fuzzy_wake_span(text)

    def find_wake_span(self, text: str) -> tuple[int, int] | None:
        t = text or ""
        m = WAKE_PATTERN.search(t)
        if m:
            return m.start(), m.end()
        return self._fuzzy_wake_span(t)

    def is_addressed(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if NEGATIVE.search(t) and not _ADDRESS_START.match(t):
            # "我们现在讨论AI算法" should not fire.
            return False
        return self.find_wake_span(t) is not None

    def is_stop_command(self, text: str, *, dialogue_active: bool = False) -> bool:
        return canonical_stop_command(text, dialogue_active=dialogue_active) is not None

    def _normalize_question(self, q: str) -> str:
        q = (q or "").strip()
        if q in _BARE_ANSWER:
            return _DEFAULT_ANSWER
        return q

    def _pending_alive(self, now: float) -> bool:
        if not self._pending:
            return False
        if self._pending_at <= 0:
            return True
        if (now - self._pending_at) > _PENDING_WINDOW_S:
            self._pending = False
            self._pending_at = 0.0
            return False
        return True

    def should_respond(self, text: str, now: float | None = None) -> bool:
        ts = float(now if now is not None else time.monotonic())
        if self.echo.should_block_wake():
            return False
        if self.echo.is_echo(text):
            return False
        t = (text or "").strip()
        if self.is_addressed(t):
            span = self.find_wake_span(t)
            remainder = t[span[1] :].lstrip("，,：: ").strip() if span else ""
            remainder = self._normalize_question(remainder)
            if not remainder:
                # Wake-name-only: next final in SAME meeting within ~15s is the question.
                self._pending = True
                self._pending_at = ts
                log.info("wake pending name-only raw=%s", t[:120])
                return False
            self._pending = False
            self._pending_at = 0.0
            return True
        if self._pending_alive(ts) and t:
            return True
        # Log likely wake attempts that did not fire (garble / blocked / expired pending).
        if t and (
            "精灵" in t
            or "精玲" in t
            or "金灵" in t
            or "金临" in t
            or "助手" in t
            or re.search(r"小?\s*AI", t, re.IGNORECASE)
        ):
            log.info("wake miss raw=%s", t[:160])
        return False

    def extract_question(self, text: str) -> str:
        t = text or ""
        span = self.find_wake_span(t)
        if span is not None:
            q = t[span[1] :].lstrip("，,：: ").strip()
            self._pending = False
            self._pending_at = 0.0
            return self._normalize_question(q)
        if self._pending:
            self._pending = False
            self._pending_at = 0.0
            return self._normalize_question(t.strip())
        return self._normalize_question(t.strip())
