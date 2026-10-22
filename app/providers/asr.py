"""Deterministic ASR provider used only by the test configuration.

Production ASR is implemented in :mod:`app.providers.funasr`; keeping this
module small prevents stale websocket/cloud adapters from being selectable.
"""

from app.providers.base import ASRProvider
from app.realtime.framing import BYTES_PER_FRAME


class FakeASRProvider(ASRProvider):
    """Deterministic ASR from injected text or PCM length."""

    DEFAULT_SCRIPT = [
        "针尖球心补偿需要重新验证。",
        "我们先采集五组数据，和旧算法对照。",
        "球心漂移的原因还没有完全确认。",
    ]

    def __init__(self, script: list[str] | None = None):
        self.script = list(script or self.DEFAULT_SCRIPT)
        self.reset()

    def start_session(self) -> None:
        self.reset()

    def feed_audio(self, pcm16: bytes) -> None:
        self._bytes += len(pcm16 or b"")
        text = self.script[self._idx % len(self.script)]
        self._partial = text[: min(len(text), max(1, self._bytes // max(BYTES_PER_FRAME, 1)))]
        if self._bytes >= BYTES_PER_FRAME * 3:
            self._final = text
            self._idx = (self._idx + 1) % len(self.script)

    def get_partial(self) -> str:
        return self._partial

    def get_final(self) -> str:
        result, self._final = self._final, ""
        if result:
            self._bytes = 0
        return result

    def flush(self) -> None:
        if self._partial and not self._final:
            self._final = self._partial

    def reset(self) -> None:
        self._idx = 0
        self._bytes = 0
        self._partial = ""
        self._final = ""

    def close(self) -> None:
        self.reset()
