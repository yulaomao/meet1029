"""FunASR adapters used by the local-only production pipeline."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

from app.logging import get_logger
from app.providers.base import ASRProvider, FinalASRProvider

log = get_logger("funasr")


class SenseVoiceRefiner:
    """Sentence-end SenseVoice-Small refinement for a live Paraformer turn.

    SenseVoice is deliberately not used as an incremental recognizer: it runs
    only after VAD/Smart Turn commits an utterance, while Paraformer continues
    to serve low-latency partial and final captions.
    """

    SAMPLE_RATE = 16000

    def __init__(self):
        self.model_id = os.environ.get("SENSEVOICE_MODEL", "iic/SenseVoiceSmall")
        self.vad_model = os.environ.get("SENSEVOICE_VAD_MODEL") or os.environ.get("FUNASR_VAD_MODEL", "fsmn-vad")
        self.device = os.environ.get("SENSEVOICE_DEVICE") or os.environ.get("FUNASR_DEVICE", "cpu")
        self.batch_size_s = max(1, int(os.environ.get("SENSEVOICE_BATCH_SIZE_S", "60")))
        self._model: Any | None = None
        # Metal/CPU inference is not re-entrant.  Live turns may complete in
        # close succession, so serialize this enhancement pass instead of
        # competing with ourselves (or loading the model twice).
        self._inference_lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from funasr import AutoModel

            kwargs = {
                "model": self.model_id,
                "vad_model": self.vad_model,
                "device": self.device,
                "disable_update": True,
            }
            try:
                self._model = AutoModel(**kwargs)
            except Exception:
                if self.device == "cpu":
                    raise
                log.warning("SenseVoice device %s unavailable; falling back to CPU", self.device, exc_info=True)
                kwargs["device"] = "cpu"
                self._model = AutoModel(**kwargs)
        return self._model

    @staticmethod
    def _plain_text(text: str) -> str:
        value = (text or "").strip()
        try:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            value = rich_transcription_postprocess(value)
        except Exception:
            # SenseVoice's language/emotion/event markers are metadata, never
            # meeting text. Retain a small local fallback if the helper changes.
            value = re.sub(r"<\|[^|]+\|>", "", value)
        return re.sub(r"\s+", " ", value).strip()

    def transcribe_pcm(self, pcm16: bytes, *, sample_rate: int = SAMPLE_RATE) -> str:
        if not pcm16:
            return ""
        with tempfile.NamedTemporaryFile(prefix="sensevoice-", suffix=".wav", delete=False) as handle:
            path = Path(handle.name)
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(int(sample_rate or self.SAMPLE_RATE))
                wav.writeframes(pcm16)
            with self._inference_lock:
                result = self._load().generate(input=str(path), batch_size_s=self.batch_size_s)
            first = result[0] if isinstance(result, list) and result else result
            first = first if isinstance(first, dict) else {}
            sentences = first.get("sentence_info") or first.get("sentences") or []
            pieces = []
            if isinstance(sentences, list):
                for row in sentences:
                    if isinstance(row, dict):
                        piece = self._plain_text(str(row.get("sentence") or row.get("text") or ""))
                        if piece:
                            pieces.append(piece)
            return "".join(pieces) if pieces else self._plain_text(str(first.get("text") or ""))
        finally:
            path.unlink(missing_ok=True)


class FunASRStreamingProvider(ASRProvider):
    """Paraformer streaming with per-meeting cache and a short revisable tail."""

    MODEL = "funasr/paraformer-zh-streaming"
    SAMPLE_RATE = 16000

    def __init__(
        self,
        model: str | None = None,
        chunk_ms: int | None = None,
        hotwords: str | None = None,
        stable_tail_chars: int | None = None,
    ):
        self.model_id = model or os.environ.get("FUNASR_STREAMING_MODEL") or self.MODEL
        self.chunk_ms = int(chunk_ms or os.environ.get("FUNASR_CHUNK_MS", "480"))
        self.stable_tail_chars = max(
            0, int(stable_tail_chars if stable_tail_chars is not None else os.environ.get("FUNASR_STABLE_TAIL_CHARS", "1"))
        )
        self.encoder_chunk_look_back = max(0, int(os.environ.get("FUNASR_ENCODER_LOOK_BACK", "4")))
        self.device = os.environ.get("FUNASR_DEVICE", "cpu")
        self._device_fallback = False
        self.hotwords = (
            str(hotwords).strip()
            if hotwords is not None
            else os.environ.get("FUNASR_HOTWORDS", "").strip()
        )
        self._model: Any | None = None
        self.reset()

    def _load(self):
        if self._model is None:
            from funasr import AutoModel

            try:
                self._model = AutoModel(model=self.model_id, device=self.device, disable_update=True)
            except Exception:
                if self.device == "cpu":
                    raise
                log.warning("FunASR device %s unavailable; falling back to CPU", self.device, exc_info=True)
                self._device_fallback = True
                self._model = AutoModel(model=self.model_id, device="cpu", disable_update=True)
        return self._model

    @staticmethod
    def _result_text(result: Any) -> str:
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return str(result[0].get("text") or "").strip()
        if isinstance(result, dict):
            return str(result.get("text") or "").strip()
        return ""

    def start_session(self) -> None:
        self.reset()

    def new_session(self) -> "FunASRStreamingProvider":
        """Create isolated stream state while reusing the loaded model weights."""
        session = FunASRStreamingProvider(
            model=self.model_id,
            chunk_ms=self.chunk_ms,
            hotwords=self.hotwords,
            stable_tail_chars=self.stable_tail_chars,
        )
        session.encoder_chunk_look_back = self.encoder_chunk_look_back
        session.device = self.device
        session._device_fallback = self._device_fallback
        session._model = self._model
        return session

    def feed_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        self._buffer.extend(pcm16)
        # Paraformer is trained with a 60-ms multiple.  [0, 8, 4] is the
        # 480-ms low-latency configuration; retain exact small chunks for unit
        # tests that substitute ``_infer``.
        if self.chunk_ms >= 480:
            target = self.SAMPLE_RATE * 2 * 8 * 60 // 1000
        else:
            target = max(1, self.SAMPLE_RATE * 2 * self.chunk_ms // 1000)
        while len(self._buffer) >= target:
            block = bytes(self._buffer[:target])
            del self._buffer[:target]
            self._infer(block, is_final=False)

    def _infer(self, pcm16: bytes, *, is_final: bool) -> None:
        if not pcm16:
            return
        import numpy as np

        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        kwargs: dict[str, Any] = {
            "input": audio,
            "cache": self._cache,
            "is_final": is_final,
            "chunk_size": [0, 8, 4],
            "encoder_chunk_look_back": self.encoder_chunk_look_back,
            "decoder_chunk_look_back": 1,
        }
        if self.hotwords:
            kwargs["hotword"] = self.hotwords
        try:
            text = self._result_text(self._load().generate(**kwargs))
        except Exception:
            log.exception("FunASR streaming inference failed")
            raise
        if text:
            self._partial = self._merge_hypothesis(self._partial, text)
            # The live UI keeps a single mutable row, so three characters are a
            # sufficient revision buffer and avoid withholding a whole sentence.
            tail = self.stable_tail_chars
            candidate = self._partial[:-tail] if tail else self._partial
            # Do not briefly publish the whole transcript when it is shorter
            # than the revision tail. That made the first update look stable,
            # then retract as soon as the tail threshold was crossed.
            if candidate.startswith(self._stable):
                self._stable = candidate
        if is_final:
            self._final = self._partial
            self._stable = ""
            self._partial = ""

    def get_partial(self) -> str:
        return self._partial

    def get_stable_prefix(self) -> str:
        return self._stable

    def get_final(self) -> str:
        final, self._final = self._final, ""
        return final

    def flush(self) -> None:
        if self._buffer:
            block, self._buffer = bytes(self._buffer), bytearray()
            self._infer(block, is_final=True)
        elif self._partial:
            self._final, self._partial = self._partial, ""
            self._stable = ""

    def reset(self) -> None:
        self._buffer = bytearray()
        self._cache: dict = {}
        self._partial = ""
        self._stable = ""
        self._final = ""

    @staticmethod
    def _merge_hypothesis(current: str, update: str) -> str:
        """Merge incremental or cumulative FunASR updates without duplicates."""
        current, update = (current or "").strip(), (update or "").strip()
        if not current:
            return update
        if not update or current.endswith(update):
            return current
        if update.startswith(current):
            return update
        overlap = min(len(current), len(update))
        for size in range(overlap, 0, -1):
            if current[-size:] == update[:size]:
                return current + update[size:]
        return current + update

    def close(self) -> None:
        self.reset()


class FunASRFinalProvider(FinalASRProvider):
    """Full-recording local ASR + FSMN-VAD; PNC runs as a guarded next stage."""

    MODEL = "paraformer-zh"

    def __init__(self, model: str | None = None):
        self.model_id = model or os.environ.get("FUNASR_FINAL_MODEL") or self.MODEL
        self.device = os.environ.get("FUNASR_DEVICE", "cpu")
        self.is_sensevoice = "sensevoice" in self.model_id.lower()
        self._model: Any | None = None
        self._vad_model: Any | None = None

    def _load(self):
        if self._model is None:
            from funasr import AutoModel

            # Punctuation is deliberately a separate post-ASR stage. Some PNC
            # checkpoints emit ``<unk>`` when chained inside AutoModel; keeping
            # raw ASR clean preserves an auditable fallback and lets the guarded
            # punctuation provider reject such artifacts.
            kwargs = {"model": self.model_id, "device": self.device, "disable_update": True}
            # Paraformer supplies sentence information itself.  SenseVoice
            # needs a separate VAD pass below so we retain timestamps for
            # diarization, evidence links and audio seeking.
            if not self.is_sensevoice:
                kwargs["vad_model"] = os.environ.get("FUNASR_VAD_MODEL", "fsmn-vad")
            try:
                self._model = AutoModel(**kwargs)
            except Exception:
                if self.device == "cpu":
                    raise
                log.warning("FunASR final device %s unavailable; falling back to CPU", self.device, exc_info=True)
                kwargs["device"] = "cpu"
                self._model = AutoModel(**kwargs)
        return self._model

    def _load_vad(self):
        if self._vad_model is None:
            from funasr import AutoModel

            model_id = os.environ.get("FUNASR_VAD_MODEL", "fsmn-vad")
            kwargs = {"model": model_id, "device": self.device, "disable_update": True}
            try:
                self._vad_model = AutoModel(**kwargs)
            except Exception:
                if self.device == "cpu":
                    raise
                log.warning("FunASR final VAD device %s unavailable; falling back to CPU", self.device, exc_info=True)
                kwargs["device"] = "cpu"
                self._vad_model = AutoModel(**kwargs)
        return self._vad_model

    @staticmethod
    def _merge_vad_regions(regions: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Join tiny VAD boundaries without making speaker turns too broad."""
        merged: list[tuple[int, int]] = []
        max_span_ms, join_gap_ms = 15000, 350
        for start, end in regions:
            start, end = max(0, int(start)), max(0, int(end))
            if end <= start:
                continue
            if merged and start - merged[-1][1] <= join_gap_ms and end - merged[-1][0] <= max_span_ms:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        return merged

    def _sensevoice_regions(self, wav_path: str) -> list[tuple[int, int]]:
        result = self._load_vad().generate(input=wav_path)
        first = result[0] if isinstance(result, list) and result else {}
        values = first.get("value") if isinstance(first, dict) else []
        regions: list[tuple[int, int]] = []
        for row in values or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                regions.append((int(row[0]), int(row[1])))
        return self._merge_vad_regions(regions)

    def _sensevoice_segment_transcript(self, wav_path: str, regions: list[tuple[int, int]]) -> list[dict]:
        """Transcribe VAD-bounded audio, retaining exact meeting offsets."""
        segments: list[dict] = []
        with wave.open(wav_path, "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise ValueError("SenseVoice final ASR expects normalized mono PCM WAV")
            sample_rate = source.getframerate()
            for start_ms, end_ms in regions:
                start_frame = int(start_ms * sample_rate / 1000)
                frame_count = max(1, int((end_ms - start_ms) * sample_rate / 1000))
                source.setpos(min(start_frame, source.getnframes()))
                frames = source.readframes(frame_count)
                if not frames:
                    continue
                with tempfile.NamedTemporaryFile(prefix="sensevoice-final-", suffix=".wav", delete=False) as handle:
                    chunk_path = Path(handle.name)
                try:
                    with wave.open(str(chunk_path), "wb") as chunk:
                        chunk.setnchannels(1)
                        chunk.setsampwidth(2)
                        chunk.setframerate(sample_rate)
                        chunk.writeframes(frames)
                    result = self._load().generate(
                        input=str(chunk_path),
                        batch_size_s=max(1, int(os.environ.get("FUNASR_FINAL_BATCH_SIZE_S", "300"))),
                    )
                    first = result[0] if isinstance(result, list) and result else {}
                    text = SenseVoiceRefiner._plain_text(str(first.get("text") or "")) if isinstance(first, dict) else ""
                    if text:
                        segments.append({"start_ms": start_ms, "end_ms": end_ms, "raw_text": text})
                finally:
                    chunk_path.unlink(missing_ok=True)
        return segments

    def _sentence_segments(self, result: dict) -> list[dict]:
        rows = result.get("sentence_info") or result.get("sentences") or []
        segments: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or row.get("sentence") or "").strip()
            if self.is_sensevoice:
                text = SenseVoiceRefiner._plain_text(text)
            if not text:
                continue
            start = int(float(row.get("start") or row.get("start_time") or 0) * 1000)
            end = int(float(row.get("end") or row.get("end_time") or 0) * 1000)
            segments.append({"start_ms": start, "end_ms": max(start, end), "raw_text": text})
        return segments

    def transcribe(self, wav_path: str, terminology: list[str] | None = None) -> dict:
        if not Path(wav_path).exists():
            raise FileNotFoundError(wav_path)
        if self.is_sensevoice:
            # SenseVoice's all-in-one API emits a text stream without stable
            # offsets.  A local VAD pass makes the final transcript usable by
            # speaker ID, exports, summaries and the audio player.
            segments = self._sensevoice_segment_transcript(wav_path, self._sensevoice_regions(wav_path))
            if segments:
                return {"text": "".join(seg["raw_text"] for seg in segments), "segments": segments}
        kwargs: dict[str, Any] = {
            "input": wav_path,
            "batch_size_s": max(1, int(os.environ.get("FUNASR_FINAL_BATCH_SIZE_S", "300"))),
        }
        # SenseVoice does not use Paraformer's hotword argument.  Passing it
        # through makes model upgrades brittle and can fail on some releases.
        if terminology and not self.is_sensevoice:
            kwargs["hotword"] = " ".join(t.strip() for t in terminology if t.strip())
        result = self._load().generate(**kwargs)
        first = result[0] if isinstance(result, list) and result else {}
        first = first if isinstance(first, dict) else {}
        text = str(first.get("text") or "").strip()
        if self.is_sensevoice:
            text = SenseVoiceRefiner._plain_text(text)
        segments = self._sentence_segments(first)
        if not segments and text:
            segments = [{"start_ms": 0, "end_ms": 0, "raw_text": text}]
        return {"text": text, "segments": segments}
