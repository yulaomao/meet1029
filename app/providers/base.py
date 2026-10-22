"""Provider interfaces. Business logic never imports MLX or CUDA directly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any


class ASRProvider(ABC):
    @abstractmethod
    def start_session(self) -> None: ...

    @abstractmethod
    def feed_audio(self, pcm16: bytes) -> None: ...

    @abstractmethod
    def get_partial(self) -> str: ...

    def get_stable_prefix(self) -> str:
        """Text safe to append in the UI; implementations may keep a revisable tail."""
        return self.get_partial()

    @abstractmethod
    def get_final(self) -> str: ...

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def flush(self) -> None:
        """Finalize buffered audio at a VAD endpoint or meeting stop."""
        return None


class DiarizationProvider(ABC):
    @abstractmethod
    def feed_audio(self, pcm16: bytes) -> None: ...

    @abstractmethod
    def current_speakers(self) -> list[dict]: ...

    @abstractmethod
    def reset(self) -> None: ...


class SpeakerIDProvider(ABC):
    @abstractmethod
    def identify(self, embedding: list[float], profiles: list[Any]) -> dict: ...


class FinalASRProvider(ABC):
    @abstractmethod
    def transcribe(self, wav_path: str, terminology: list[str] | None = None) -> dict: ...


class TTSProvider(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> bytes: ...

    def stream(self, text: str) -> Iterator[bytes]:
        yield self.synthesize(text)


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, prompt: str, *, json_mode: bool = False, model: str | None = None) -> str: ...

    def stream(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cancel_event: Any | None = None,
    ) -> Iterator[str]:
        yield self.complete(prompt, model=model)


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def embed_audio(self, data: bytes) -> list[float]:
        import hashlib
        return self.embed(hashlib.sha256(data or b"").hexdigest())


class VADProvider(ABC):
    @abstractmethod
    def is_speech(self, pcm16: bytes) -> bool: ...


class NotificationProvider(ABC):
    @abstractmethod
    def send(self, recipient: str, title: str, content: str) -> None: ...
