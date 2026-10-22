"""Select local-only providers from SPEECH_BACKEND / LLM_BACKEND."""

from __future__ import annotations

import sys

from app.providers.asr import FakeASRProvider
from app.providers.diarization import EmbeddingClusterProvider, FakeDiarizationProvider, SortformerProvider
from app.providers.embedding import CampPlusSpeakerEmbeddingProvider, FakeEmbeddingProvider
from app.providers.final_asr import FakeFinalASRProvider
from app.providers.funasr import FunASRFinalProvider, FunASRStreamingProvider, SenseVoiceRefiner
from app.providers.llm import FakeLLMProvider, Qwen38LocalProvider
from app.providers.notification import InAppNotificationProvider
from app.providers.punctuation import FakePunctuationProvider, FunASRPunctuationProvider
from app.providers.speaker_id import CosineSpeakerIDProvider
from app.providers.tts import FakeTTSProvider, MacSayTTSProvider, Qwen3TTSMLXProvider, Qwen3TTSTorchProvider
from app.providers.vad import FakeVAD, build_vad


class ProviderRegistry:
    def __init__(self, speech_backend: str = "fake", llm_backend: str = "fake"):
        self.speech_backend = (speech_backend or "fake").lower()
        self.llm_backend = (llm_backend or "fake").lower()
        self.asr = self._asr()
        self.sensevoice = self._sensevoice()
        self.diarization = self._diarization()
        self.speaker_id = CosineSpeakerIDProvider()
        self.final_asr = self._final_asr()
        self.punctuation = self._punctuation()
        self.tts = self._tts()
        self.llm = self._llm()
        self.embedding = self._embedding()
        self.vad = FakeVAD() if self.speech_backend == "fake" else build_vad()
        self.notification = InAppNotificationProvider()

    def _asr(self):
        if self.speech_backend == "funasr":
            return FunASRStreamingProvider()
        return FakeASRProvider()

    def _sensevoice(self):
        import os

        enabled = os.environ.get("SENSEVOICE_REFINEMENT_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        return SenseVoiceRefiner() if self.speech_backend == "funasr" and enabled else None

    def _diarization(self):
        if self.speech_backend == "funasr":
            # Actual live labels are produced at completed VAD turns by
            # CAM++ embeddings plus StickySpeakerAssigner clustering.  Do not
            # advertise the placeholder Sortformer class as a working model.
            return EmbeddingClusterProvider()
        return FakeDiarizationProvider()

    def _final_asr(self):
        if self.speech_backend == "funasr":
            return FunASRFinalProvider()
        return FakeFinalASRProvider()

    def _punctuation(self):
        if self.speech_backend == "funasr":
            return FunASRPunctuationProvider()
        return FakePunctuationProvider()

    def _tts(self):
        import os

        explicit = (os.environ.get("TTS_BACKEND") or "").strip().lower()
        if explicit == "fake" or (self.speech_backend == "fake" and explicit not in {"say", "macos"}):
            return FakeTTSProvider()
        if explicit in {"qwen3", "qwen3_torch", "torch"}:
            return Qwen3TTSTorchProvider()
        if self.speech_backend == "mlx" or explicit == "mlx":
            try:
                return Qwen3TTSMLXProvider()
            except Exception:
                pass
        if sys.platform == "darwin" and explicit in {"", "say", "macos", "auto"}:
            try:
                return MacSayTTSProvider()
            except Exception:
                pass
        return FakeTTSProvider()

    def _llm(self):
        if self.llm_backend == "qwen38":
            return Qwen38LocalProvider()
        return FakeLLMProvider()

    def _embedding(self):
        if self.speech_backend == "funasr":
            try:
                return CampPlusSpeakerEmbeddingProvider()
            except Exception:
                return FakeEmbeddingProvider()
        return FakeEmbeddingProvider()


def _cfg_get(config, key: str, default: str = "fake") -> str:
    """Read from a Flask config mapping or a Config class/object."""
    if not isinstance(config, type):
        getter = getattr(config, "get", None)
        if callable(getter):
            val = getter(key)
            if val is not None:
                return val
    return getattr(config, key, default)


def build_registry(config) -> ProviderRegistry:
    return ProviderRegistry(
        speech_backend=_cfg_get(config, "SPEECH_BACKEND", "fake"),
        llm_backend=_cfg_get(config, "LLM_BACKEND", "fake"),
    )
