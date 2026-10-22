"""Local FunASR punctuation providers.

The online model receives only newly stable text and keeps its cache for one
meeting.  The offline model is used once an utterance reaches a VAD endpoint.
"""

from __future__ import annotations

import os
from typing import Any

from app.logging import get_logger

log = get_logger("punctuation")


class FunASRPunctuationProvider:
    ONLINE_MODEL = "iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727"
    FINAL_MODEL = "iic/punc_ct-transformer_cn-en-common-vocab471067-large"

    def __init__(self, online_model: str | None = None, final_model: str | None = None):
        self.online_model_id = online_model or os.environ.get("FUNASR_ONLINE_PUNC_MODEL") or self.ONLINE_MODEL
        self.final_model_id = final_model or os.environ.get("FUNASR_FINAL_PUNC_MODEL") or self.FINAL_MODEL
        self._online_model: Any | None = None
        self._final_model: Any | None = None
        self._cache: dict = {}
        self._seen = ""
        self._rendered = ""

    @property
    def _device(self) -> str:
        return os.environ.get("FUNASR_DEVICE", "cpu")

    @staticmethod
    def _text(result: Any, fallback: str) -> str:
        text = ""
        if isinstance(result, list) and result and isinstance(result[0], dict):
            text = str(result[0].get("text") or "").strip()
        elif isinstance(result, dict):
            text = str(result.get("text") or "").strip()
        # Never publish a punctuation model's unknown-token artifact as a
        # correction. The raw ASR remains the safe, auditable fallback.
        if not text or "<unk>" in text.lower() or "<" in text or ">" in text:
            return fallback.strip()
        return text

    def _load_online(self):
        if self._online_model is None:
            from funasr import AutoModel

            self._online_model = AutoModel(model=self.online_model_id, device=self._device, disable_update=True)
        return self._online_model

    def _load_final(self):
        # The local Chinese checkpoint is suitable for both online and endpoint
        # punctuation. Reuse the loaded instance to avoid a second 278 MB copy.
        if self.final_model_id == self.online_model_id:
            return self._load_online()
        if self._final_model is None:
            from funasr import AutoModel

            self._final_model = AutoModel(model=self.final_model_id, device=self._device, disable_update=True)
        return self._final_model

    def online(self, stable_text: str) -> str:
        stable_text = (stable_text or "").strip()
        if not stable_text:
            return ""
        if stable_text == self._seen:
            return self._rendered or stable_text
        try:
            # The locally cached PNC checkpoint emits only its input delta when
            # called with a cache. Re-punctuating the short, stable prefix keeps
            # one coherent mutable caption instead of stitching two sentences.
            text = self._text(self._load_online().generate(input=stable_text), stable_text)
        except Exception:
            log.warning("online punctuation unavailable; preserving ASR text", exc_info=True)
            text = stable_text
        self._seen = stable_text
        self._rendered = text
        return self._rendered

    def finalize(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        try:
            return self._text(self._load_final().generate(input=text), text)
        except Exception:
            log.warning("offline punctuation unavailable; preserving ASR text", exc_info=True)
            return text

    def reset(self) -> None:
        self._cache = {}
        self._seen = ""
        self._rendered = ""

    def new_session(self) -> "FunASRPunctuationProvider":
        """Isolate online punctuation state while reusing loaded model weights."""
        session = FunASRPunctuationProvider(self.online_model_id, self.final_model_id)
        session._online_model = self._online_model
        session._final_model = self._final_model
        return session


class FakePunctuationProvider:
    """Tests: preserve supplied punctuation and provide a deterministic final stop."""

    def online(self, stable_text: str) -> str:
        return (stable_text or "").strip()

    def finalize(self, text: str) -> str:
        text = (text or "").strip()
        if text and text[-1] not in "，。？！；：":
            return text + "。"
        return text

    def reset(self) -> None:
        return None
