"""Echo cancellation helpers.

1. Browser echoCancellation (documented in JS getUserMedia constraints)
2. AI text stored as speaker=成就精灵
3. Block wake while TTS is playing
4. Filter ASR that is highly similar to last TTS text
"""

from __future__ import annotations

import difflib
import re
from collections import deque

AI_SPEAKER = "成就精灵"


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


class EchoFilter:
    def __init__(self, threshold: float = 0.72):
        self.threshold = threshold
        self.last_tts: str = ""
        # One answer can now be synthesized in several streaming units.  Keep
        # a bounded transcript of the units that are still audible, instead
        # of only the last unit, otherwise echo from an earlier unit can be
        # mistaken for a participant's interruption.
        self._tts_history: deque[str] = deque(maxlen=12)
        self.ai_speaking: bool = False

    def on_tts(self, text: str) -> None:
        value = text or ""
        self.last_tts = value
        if value:
            self._tts_history.append(value)
        self.ai_speaking = True

    def begin_turn(self) -> None:
        """Forget a completed answer before starting a new assistant turn."""
        self.last_tts = ""
        self._tts_history.clear()
        self.ai_speaking = True

    def on_tts_end(self) -> None:
        self.ai_speaking = False

    def is_echo(self, asr_text: str) -> bool:
        candidates = list(self._tts_history) or ([self.last_tts] if self.last_tts else [])
        if not candidates:
            return False
        heard = normalize(asr_text)
        if not heard:
            return False
        for candidate in candidates:
            spoken = normalize(candidate)
            if not spoken:
                continue
            # A microphone usually receives only a short slice of the
            # assistant's answer. Comparing it with the whole answer makes it
            # look dissimilar and can turn the assistant's own voice into a
            # barge-in.
            if len(heard) >= 5 and heard in spoken:
                return True
            pieces = [normalize(piece) for piece in re.split(r"[，。！？；、,.!?;:\\n]+", candidate)]
            if any(len(piece) >= 5 and similarity(heard, piece) >= self.threshold for piece in pieces):
                return True
            if similarity(heard, spoken) >= self.threshold:
                return True
        return False

    def should_block_wake(self) -> bool:
        return self.ai_speaking

    def filter_asr(self, asr_text: str) -> str | None:
        """Return None if the ASR hypothesis should be dropped as echo."""
        if self.is_echo(asr_text):
            return None
        return asr_text
