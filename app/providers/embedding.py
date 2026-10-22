"""Text and local speaker embeddings."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from app.providers.base import EmbeddingProvider
from app.media import resolve_ffmpeg

DIM = 64


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dim: int = DIM):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = (text or "").lower().replace("，", " ").replace("。", " ").split()
        if not tokens:
            tokens = list(text or " ")
        for tok in tokens:
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:2], "little") % self.dim
            sign = 1.0 if h[2] % 2 == 0 else -1.0
            vec[idx] += sign
        n = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / n for x in vec]

    def embed_audio(self, data: bytes) -> list[float]:
        digest = hashlib.sha256(data or b"").hexdigest()
        return self.embed(digest)


class CampPlusSpeakerEmbeddingProvider(EmbeddingProvider):
    """Local Chinese CAM++ speaker vectors from the cached FunASR model.

    This deliberately remains on CPU: the installed ModelScope speaker pipeline
    rejects PyTorch MPS devices. It runs only for a completed human utterance
    when at least one enrolled profile exists, while ASR and TTS stay on Metal.
    """

    DIM = 192

    def __init__(self, model: str | None = None):
        self.model_id = model or os.environ.get("FUNASR_SPEAKER_MODEL") or ""
        self._pipeline = None
        self._lock = threading.Lock()

    def embed(self, text: str) -> list[float]:
        # Speaker-ID callers use embed_audio. Keep the base contract useful for
        # code paths that ask an embedding provider for text.
        return FakeEmbeddingProvider(dim=self.DIM).embed(text)

    def _load(self):
        if self._pipeline is None:
            model_path = Path(self.model_id).expanduser()
            if not model_path.exists():
                raise FileNotFoundError(f"local CAM++ speaker model is missing: {model_path}")
            with self._lock:
                if self._pipeline is None:
                    from modelscope.pipelines import pipeline

                    self._pipeline = pipeline("speaker-verification", model=str(model_path), device="cpu")
        return self._pipeline

    @staticmethod
    def _suffix(mime: str | None) -> str:
        raw = (mime or "").lower()
        if "wav" in raw:
            return ".wav"
        if "mp4" in raw or "m4a" in raw:
            return ".m4a"
        if "ogg" in raw:
            return ".ogg"
        return ".webm"

    def embed_audio(self, data: bytes, *, mime: str | None = None) -> list[float]:
        if not data:
            return []
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory(prefix="meeting-speaker-") as td:
            work = Path(td)
            if data.startswith(b"RIFF"):
                source = work / "source.wav"
                source.write_bytes(data)
                wav_path = source
            elif mime and ("webm" in mime.lower() or "ogg" in mime.lower() or "mp4" in mime.lower() or "m4a" in mime.lower()):
                source = work / f"source{self._suffix(mime)}"
                source.write_bytes(data)
                wav_path = work / "normalized.wav"
                ffmpeg = resolve_ffmpeg()
                if not ffmpeg:
                    raise RuntimeError("ffmpeg is required to decode enrollment audio")
                proc = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source), "-ac", "1", "-ar", "16000", str(wav_path)],
                    capture_output=True,
                    check=False,
                    timeout=45,
                )
                if proc.returncode or not wav_path.exists():
                    raise RuntimeError("could not decode enrollment audio")
            else:
                # Live AudioWorklet frames are signed 16-bit, mono, 16 kHz PCM.
                samples = np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2").astype(np.float32) / 32768.0
                if not len(samples):
                    return []
                wav_path = work / "live.wav"
                sf.write(wav_path, samples, 16000, subtype="PCM_16")
            result = self._load()([str(wav_path)], output_emb=True)
            vectors = result.get("embs") if isinstance(result, dict) else None
            if vectors is None or len(vectors) < 1:
                return []
            return [float(x) for x in vectors[0].tolist()]

    def warmup(self) -> None:
        """Load CAM++ and exercise its inference path before the first meeting."""
        # A short silent PCM sample initializes ModelScope and the CPU inference
        # kernels without enrolling or persisting a voice identity.
        self.embed_audio(b"\x00\x00" * 16000 * 2)


class SentenceTransformerEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("sentence-transformers extra is not installed") from exc
        self.model = SentenceTransformer(model_name)

    def embed(self, text: str) -> list[float]:
        return self.model.encode([text])[0].tolist()
