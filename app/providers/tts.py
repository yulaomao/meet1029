"""Local TTS providers, including streaming Qwen3-TTS on Apple Silicon."""

from __future__ import annotations

import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from io import BytesIO
from pathlib import Path

from app.exceptions import PlatformNotSupported
from app.logging import get_logger
from app.providers.base import TTSProvider

log = get_logger("tts")


_MD_EMPHASIS = re.compile(r"(`+|\*{1,3}|_{1,3})(.*?)\1")
_MD_LIST_PREFIX = re.compile(r"(?:^|\n)\s*(?:[-*+]\s+|(\d+)[.)、]\s+)")
_WHITESPACE = re.compile(r"\s+")
_UNIT_NUMBER = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(nm|毫米|mm|厘米|cm|米|m|千克|kg|克|g|毫秒|ms|秒|s)(?![A-Za-z])", re.I)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U0002fa1f]")
_AFFECTIVE_TTS_LEAD = re.compile(
    r"^(?:(?:嗯|啊|哦|诶|呃)+|哈{2,}|(?:好的|当然|没问题|收到))[，、!！。\s]*"
)


def _speak_number(value: str) -> str:
    """Pronounce small measurement numbers naturally instead of character by character."""
    digits = "零一二三四五六七八九"
    if "." in value:
        left, right = value.split(".", 1)
        return _speak_number(left) + "点" + "".join(digits[int(x)] for x in right if x.isdigit())
    try:
        number = int(value)
    except ValueError:
        return value
    if number < 0 or number >= 10000:
        return "".join(digits[int(x)] for x in value if x.isdigit())
    if number < 10:
        return digits[number]
    units = ((1000, "千"), (100, "百"), (10, "十"), (1, ""))
    remaining, out, pending_zero = number, [], False
    for divisor, unit in units:
        digit, remaining = divmod(remaining, divisor)
        if digit:
            if pending_zero:
                out.append("零")
            if divisor == 10 and digit == 1 and not out:
                out.append("十")
            else:
                out.append(digits[digit] + unit)
            pending_zero = False
        elif out and remaining:
            pending_zero = True
    return "".join(out) or "零"


def normalize_tts_text(text: str) -> str:
    """Turn an LLM/Markdown answer into short, stable spoken Chinese.

    The UI retains the original answer.  Only the audio route is normalized:
    Markdown, list indentation and machine-style units otherwise make an
    expressive TTS model infer exaggerated pauses and a slow cadence.
    """
    spoken = (text or "").replace("\r", "\n").strip()
    if not spoken:
        return ""
    spoken = _MD_EMPHASIS.sub(lambda match: match.group(2), spoken)
    spoken = re.sub(r"^\s{0,3}#{1,6}\s*", "", spoken, flags=re.M)

    def list_prefix(match: re.Match) -> str:
        ordinal = match.group(1)
        return "。第" + _speak_number(ordinal) + "，" if ordinal else "。"

    spoken = _MD_LIST_PREFIX.sub(list_prefix, spoken)
    spoken = spoken.replace("|", "，").replace("~", "")
    unit_names = {"nm": "纳米", "mm": "毫米", "cm": "厘米", "m": "米", "kg": "千克", "g": "克", "ms": "毫秒", "s": "秒"}
    spoken = _UNIT_NUMBER.sub(
        lambda match: _speak_number(match.group(1)) + unit_names.get(match.group(2).lower(), match.group(2)),
        spoken,
    )
    spoken = _WHITESPACE.sub(" ", spoken)
    spoken = re.sub(r"(?<=[\u4e00-\u9fff])\s+|\s+(?=[\u4e00-\u9fff])", "", spoken)
    spoken = re.sub(r"\s*([，。！？；：])\s*", r"\1", spoken)
    spoken = re.sub(r"[：:]。", "：", spoken)
    return re.sub(r"([。！？]){2,}", r"\1", spoken).strip(" ，；")


def normalize_meeting_tts_text(text: str) -> str:
    """Remove delivery cues that make a meeting assistant sound performative.

    The visible LLM answer is untouched.  Qwen3-TTS receives a restrained
    spoken rendering: no verbal laugh/acknowledgement lead-in and no emphatic
    exclamation marks that would otherwise reset prosody in a later streamed
    synthesis request.  Real questions retain their question mark.
    """
    spoken = normalize_tts_text(text)
    if not spoken:
        return ""
    neutral = spoken
    # A reply can begin with more than one conversational marker (for example
    # “哈哈！好的，…”); remove only the consecutive lead-ins, never content in
    # the body of the answer.
    while True:
        trimmed = _AFFECTIVE_TTS_LEAD.sub("", neutral, count=1)
        if trimmed == neutral:
            break
        neutral = trimmed
    neutral = re.sub(r"[!！]+", "。", neutral)
    neutral = re.sub(r"(?:…|\.{3,})+", "，", neutral)
    neutral = re.sub(r"([，、]){2,}", "，", neutral)
    neutral = re.sub(r"([。？]){2,}", r"\1", neutral).strip(" ，；")
    return neutral or spoken


def _sine_wav(duration_s: float = 0.4, freq: float = 440.0, rate: int = 16000) -> bytes:
    n = int(rate * duration_s)
    pcm = []
    for i in range(n):
        sample = int(32767 * 0.15 * math.sin(2 * math.pi * freq * i / rate))
        pcm.append(sample)
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<" + "h" * n, *pcm))
    return buf.getvalue()


class FakeTTSProvider(TTSProvider):
    def synthesize(self, text: str) -> bytes:
        dur = min(2.0, 0.15 + 0.02 * len(text or ""))
        return _sine_wav(duration_s=dur)


class MacSayTTSProvider(TTSProvider):
    """System say -> 16 kHz mono WAV. No extra model download."""

    SAMPLE_RATE = 16000

    def __init__(self, voice: str | None = None):
        if sys.platform != "darwin":
            raise PlatformNotSupported("MacSayTTSProvider", sys.platform)
        self.voice = voice or os.environ.get("TTS_VOICE") or self._pick_voice()

    @staticmethod
    def _pick_voice() -> str:
        preferred = ("Tingting", "Eddy (中文（中国大陆）)", "Meijia")
        try:
            out = subprocess.check_output(["say", "-v", "?"], text=True, timeout=5, stderr=subprocess.STDOUT)
        except Exception:
            return "Tingting"
        for name in preferred:
            if name in out:
                return name
        for line in out.splitlines():
            if "zh_CN" in line:
                return line.split()[0]
        return "Tingting"

    def synthesize(self, text: str) -> bytes:
        spoken = (text or "").replace("[[", " ").strip()
        if not spoken:
            return b""
        with tempfile.TemporaryDirectory(prefix="meeting-tts-") as td:
            wav_path = Path(td) / "out.wav"
            cmd = [
                "say",
                "-v",
                self.voice,
                "-o",
                str(wav_path),
                "--data-format=LEI16@16000",
                "--file-format=WAVE",
                spoken,
            ]
            try:
                subprocess.run(cmd, check=True, timeout=45, capture_output=True)
            except Exception:
                log.warning("mac say tts failed voice=%s", self.voice, exc_info=True)
                return _sine_wav(duration_s=min(2.0, 0.2 + 0.03 * len(spoken)))
            data = wav_path.read_bytes() if wav_path.exists() else b""
            if len(data) > 44 and data.startswith(b"RIFF"):
                log.info("mac say tts ok bytes=%s voice=%s chars=%s", len(data), self.voice, len(spoken))
                return data
            log.warning("mac say tts empty wav bytes=%s", len(data))
            return _sine_wav(duration_s=min(2.0, 0.2 + 0.03 * len(spoken)))


class Qwen3TTSMLXProvider(TTSProvider):
    """Qwen3-TTS CustomVoice via MLX-Audio.

    ``stream`` yields raw 16-bit PCM chunks at ``SAMPLE_RATE`` so the live
    browser can begin playback before the complete answer has been generated.
    ``synthesize`` remains available for non-live API callers and joins those
    same chunks into a WAV.
    """

    MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit"
    # Keep the browser transport deterministic.  Qwen3-TTS generates at 24k,
    # but the realtime pipeline (and the reference speech-to-speech project)
    # sends fixed 16k PCM blocks to one long-lived player queue.
    SAMPLE_RATE = 16000
    STREAMS_PCM16 = True
    BLOCK_SAMPLES = 512
    MIN_UTTERANCE_TOKENS = 360
    MAX_UTTERANCE_TOKENS = 1536
    TOKENS_PER_SECOND = 12.5

    def __init__(self):
        if sys.platform != "darwin":
            raise PlatformNotSupported("Qwen3TTSMLXProvider", sys.platform)
        try:
            import mlx_audio  # noqa: F401
        except ImportError as exc:
            raise PlatformNotSupported("Qwen3TTSMLXProvider", "mlx-audio missing") from exc
        self.model_id = os.environ.get("TTS_MLX_MODEL") or self.MODEL
        # Match the reference project's CustomVoice defaults.  A strong style
        # prompt and aggressively narrowed sampling made every independently
        # synthesized sentence sound like a different constrained performance.
        self.voice = (os.environ.get("TTS_MLX_VOICE") or "aiden").strip().lower()
        self.language = (os.environ.get("TTS_MLX_LANGUAGE") or "auto").strip().lower()
        self.instruct = (os.environ.get("TTS_MLX_INSTRUCT") or "").strip() or None
        self.streaming_interval = max(0.08, float(os.environ.get("TTS_MLX_STREAM_INTERVAL", "0.32")))
        self.repetition_penalty = min(2.0, max(1.0, float(os.environ.get("TTS_MLX_REPETITION_PENALTY", "1.20"))))
        self.max_tokens_cap = max(self.MIN_UTTERANCE_TOKENS, int(os.environ.get("TTS_MLX_MAX_TOKENS", str(self.MAX_UTTERANCE_TOKENS))))
        # Advanced sampling remains opt-in for troubleshooting; the reference
        # profile deliberately lets mlx-audio use its stable defaults.
        self.temperature = self._optional_float("TTS_MLX_TEMPERATURE", 0.05, 1.0)
        self.top_k = self._optional_int("TTS_MLX_TOP_K", 1)
        self.top_p = self._optional_float("TTS_MLX_TOP_P", 0.05, 1.0)
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from mlx_audio.tts.utils import load_model

            model_path = Path(self.model_id).expanduser()
            if not model_path.exists():
                # Model IDs are useful for development, but production .env
                # points at a verified local snapshot; do not silently fetch.
                raise FileNotFoundError(f"local MLX TTS model is missing: {model_path}")
            with self._lock:
                if self._model is None:
                    self._model = load_model(model_path)
                    supported = {str(x).lower() for x in self._model.get_supported_speakers()}
                    if self.voice not in supported:
                        self.voice = "aiden" if "aiden" in supported else next(iter(supported))
                    self._model_sample_rate = int(getattr(self._model, "sample_rate", 24000) or 24000)
                    log.info("mlx qwen3 tts loaded model=%s voice=%s model_rate=%s output_rate=%s", model_path, self.voice, self._model_sample_rate, self.SAMPLE_RATE)
        return self._model

    @staticmethod
    def _optional_float(name: str, minimum: float, maximum: float) -> float | None:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return None
        return min(maximum, max(minimum, float(raw)))

    @staticmethod
    def _optional_int(name: str, minimum: int) -> int | None:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return None
        return max(minimum, int(raw))

    @staticmethod
    def _pcm16(audio) -> bytes:
        import numpy as np

        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not len(samples):
            return b""
        samples = np.clip(samples, -1.0, 1.0)
        return (samples * 32767.0).astype("<i2").tobytes()

    def _estimate_max_tokens(self, spoken: str) -> int:
        """Reference-compatible, language-aware Qwen codec-token budget."""
        compact = re.sub(r"\s+", "", spoken or "")
        words = len(re.findall(r"\w+", spoken or "", flags=re.UNICODE))
        cjk = len(_CJK.findall(spoken or ""))
        punctuation = sum(1 for ch in spoken or "" if ch in "，。！？；：、,.!?;:")
        seconds = max(words / 2.6 if words else 0.0, len(compact) / 14.0 if compact else 0.0, cjk / 5.5 if cjk else 0.0)
        requested = math.ceil((seconds + punctuation * 0.5 + 1.0) * self.TOKENS_PER_SECOND * 1.35)
        aligned = max(1, math.ceil(requested / 4) * 4)
        return min(self.max_tokens_cap, max(self.MIN_UTTERANCE_TOKENS, aligned))

    @staticmethod
    def _resample(samples, source_rate: int, target_rate: int):
        import numpy as np

        source = np.asarray(samples, dtype=np.float32).reshape(-1)
        if source_rate == target_rate or not len(source):
            return source.copy()
        try:
            from math import gcd
            from scipy.signal import resample_poly

            divisor = gcd(source_rate, target_rate)
            return np.asarray(resample_poly(source, target_rate // divisor, source_rate // divisor), dtype=np.float32)
        except Exception:
            size = max(1, round(len(source) * target_rate / source_rate))
            return np.interp(np.linspace(0, len(source) - 1, size), np.arange(len(source)), source).astype(np.float32)

    def _stream_pcm_blocks(self, results):
        """Sanitize MLX scratch buffers and emit fixed 32ms PCM packets."""
        import numpy as np

        first_audio = True
        found_speech = False
        leftover = np.empty(0, dtype=np.int16)
        previous: np.ndarray | None = None
        repeat_run = 0
        for result in results:
            source = np.array(getattr(result, "audio", []), dtype=np.float32, copy=True).reshape(-1)
            if not len(source) or not np.isfinite(source).all():
                continue
            if float(np.max(np.abs(source))) > 1.5:
                source /= 32768.0
            source = self._resample(source, int(getattr(result, "sample_rate", 0) or getattr(self, "_model_sample_rate", 24000)), self.SAMPLE_RATE)
            current = np.clip(source, -1.0, 1.0)
            current = (current * 32767.0).astype("<i2")
            if previous is not None and current.size == previous.size and current.size:
                mad = float(np.mean(np.abs(current.astype(np.int32) - previous.astype(np.int32))))
                if mad < 48:
                    repeat_run += 1
                    if repeat_run >= 2:
                        log.info("dropping repeated qwen3 tts pcm chunk mad=%.1f run=%s", mad, repeat_run)
                        # A repeated decoder packet must never terminate the
                        # whole utterance.  That used to turn a short run of
                        # duplicate MLX buffers into an audible mid-sentence
                        # stop.  Drop the duplicate and reset the comparison
                        # window so a following non-duplicate packet can
                        # still reach the browser.
                        if repeat_run >= 3:
                            previous = None
                            repeat_run = 0
                        continue
                else:
                    repeat_run = 0
            else:
                repeat_run = 0
            previous = current
            if not found_speech:
                above = np.flatnonzero(np.abs(current) > int(32768 * 0.01))
                if not len(above):
                    continue
                current = current[max(0, int(above[0]) - int(self.SAMPLE_RATE * 0.04)) :]
                found_speech = True
            merged = np.concatenate((leftover, current))
            usable = len(merged) - (len(merged) % self.BLOCK_SAMPLES)
            for offset in range(0, usable, self.BLOCK_SAMPLES):
                yield merged[offset : offset + self.BLOCK_SAMPLES].tobytes()
            leftover = merged[usable:]
            first_audio = False
        if len(leftover) and int(np.max(np.abs(leftover))) >= 40:
            yield np.pad(leftover, (0, self.BLOCK_SAMPLES - len(leftover))).astype("<i2").tobytes()

    def warmup(self) -> None:
        """Pay model/JIT startup before the first question, not during it."""
        iterator = self.stream("你好。")
        next(iterator, b"")

    def stream(self, text: str):
        spoken = normalize_meeting_tts_text((text or "").replace("[[", " "))
        if not spoken:
            return
        model = self._load()
        max_tokens = self._estimate_max_tokens(spoken)
        try:
            kwargs = {
                "speaker": self.voice,
                "language": self.language,
                "repetition_penalty": self.repetition_penalty,
                "max_tokens": max_tokens,
                "stream": True,
                "streaming_interval": self.streaming_interval,
            }
            if self.instruct:
                kwargs["instruct"] = self.instruct
            for name in ("temperature", "top_k", "top_p"):
                value = getattr(self, name)
                if value is not None:
                    kwargs[name] = value
            yield from self._stream_pcm_blocks(model.generate_custom_voice(spoken, **kwargs))
        except Exception:
            log.exception("mlx qwen3 tts failed chars=%s", len(spoken))
            raise

    def synthesize(self, text: str) -> bytes:
        pcm = b"".join(self.stream(text))
        if not pcm:
            return b""
        out = BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(pcm)
        return out.getvalue()


class Qwen3TTSTorchProvider(TTSProvider):
    """Qwen3-TTS CustomVoice on CUDA for non-Apple hosts.

    Qwen's PyTorch API returns a complete waveform.  This adapter converts it
    to the app's 16 kHz PCM transport and yields fixed-size blocks after
    generation, while keeping the same provider interface as the MLX path.
    """

    SAMPLE_RATE = 16000
    STREAMS_PCM16 = True
    BLOCK_SAMPLES = 512
    MODEL = ".local/models/qwen3-tts-1.7b-customvoice"

    def __init__(self):
        self.model_id = os.environ.get("TTS_TORCH_MODEL") or self.MODEL
        self.voice = (os.environ.get("TTS_TORCH_VOICE") or "Serena").strip()
        self.language = (os.environ.get("TTS_TORCH_LANGUAGE") or "Chinese").strip()
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            import torch
            from qwen_tts import Qwen3TTSModel

            if not torch.cuda.is_available():
                raise RuntimeError("Qwen3-TTS CUDA backend selected, but PyTorch cannot access a CUDA GPU")
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            with self._lock:
                if self._model is None:
                    self._model = Qwen3TTSModel.from_pretrained(
                        self.model_id,
                        device_map="cuda:0",
                        dtype=dtype,
                    )
                    log.info("torch qwen3 tts loaded model=%s voice=%s", self.model_id, self.voice)
        return self._model

    def stream(self, text: str):
        spoken = normalize_meeting_tts_text((text or "").replace("[[", " "))
        if not spoken:
            return
        import numpy as np

        model = self._load()
        with self._lock:
            waveforms, sample_rate = model.generate_custom_voice(
                text=spoken,
                language=self.language,
                speaker=self.voice,
            )
        if not waveforms:
            return
        audio = np.asarray(waveforms[0], dtype=np.float32).squeeze()
        if audio.ndim == 2:
            audio = audio.mean(axis=0 if audio.shape[0] < audio.shape[1] else 1)
        if not audio.size or not np.isfinite(audio).all():
            return
        audio = Qwen3TTSMLXProvider._resample(audio, int(sample_rate or 24000), self.SAMPLE_RATE)
        pcm = Qwen3TTSMLXProvider._pcm16(audio)
        block_bytes = self.BLOCK_SAMPLES * 2
        for offset in range(0, len(pcm), block_bytes):
            block = pcm[offset : offset + block_bytes]
            if len(block) < block_bytes:
                block += b"\x00" * (block_bytes - len(block))
            yield block

    def synthesize(self, text: str) -> bytes:
        pcm = b"".join(self.stream(text))
        if not pcm:
            return b""
        out = BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(pcm)
        return out.getvalue()

    def warmup(self) -> None:
        next(self.stream("你好。"), None)
