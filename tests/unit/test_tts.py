"""TTS providers produce audible bytes; MacSay is Mac-only."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from app.exceptions import PlatformNotSupported
from app.providers.tts import (
    FakeTTSProvider,
    MacSayTTSProvider,
    Qwen3TTSMLXProvider,
    _sine_wav,
    normalize_meeting_tts_text,
    normalize_tts_text,
)


def test_fake_tts_returns_wav_bytes():
    audio = FakeTTSProvider().synthesize("刚才没接上，请再说一次")
    assert audio.startswith(b"RIFF")
    assert len(audio) > 44


def test_tts_text_normalization_removes_markdown_and_speaks_measurement_units():
    raw = "当前要点：\n\n1. **系统精度**需要复验。\n- 用850nm近红外相机检查。"
    spoken = normalize_tts_text(raw)
    assert "**" not in spoken
    assert "850nm" not in spoken
    assert "八百五十纳米" in spoken
    assert "第一" in spoken


def test_meeting_tts_text_omits_affective_leadin_and_emphatic_delivery_marks():
    spoken = normalize_meeting_tts_text("哈哈！好的，**这个方案**可以继续推进！")
    assert spoken == "这个方案可以继续推进。"


def test_qwen_tts_uses_bounded_sampling_and_acoustic_token_budget():
    captured = {}

    class FakeModel:
        sample_rate = 24000

        def generate_custom_voice(self, text, **kwargs):
            captured["text"] = text
            captured.update(kwargs)
            yield SimpleNamespace(audio=np.array([0.0, 0.1], dtype=np.float32), token_count=3)

    provider = object.__new__(Qwen3TTSMLXProvider)
    provider.voice = "aiden"
    provider.language = "auto"
    provider.instruct = None
    provider.temperature = None
    provider.top_k = None
    provider.top_p = None
    provider.repetition_penalty = 1.2
    provider.max_tokens_cap = 1536
    provider.streaming_interval = 0.32
    provider._model_sample_rate = 24000
    provider._load = lambda: FakeModel()
    assert list(provider.stream("**检查850nm** 光污染。"))
    assert captured["text"] == "检查八百五十纳米光污染。"
    assert captured["speaker"] == "aiden"
    assert captured["language"] == "auto"
    assert "instruct" not in captured
    assert "temperature" not in captured
    assert captured["repetition_penalty"] == 1.2
    assert 360 <= captured["max_tokens"] <= 1536


def test_sine_wav_is_pcm():
    blob = _sine_wav(duration_s=0.2)
    assert blob[8:12] == b"WAVE"
    assert len(blob) > 1000


def test_repeated_decoder_packets_do_not_end_the_rest_of_an_utterance():
    """A short duplicate run is removed without cutting following audio."""
    provider = object.__new__(Qwen3TTSMLXProvider)
    provider.SAMPLE_RATE = 16000
    provider.BLOCK_SAMPLES = 512
    provider._model_sample_rate = 16000
    provider._resample = lambda audio, _src, _dst: audio
    repeated = np.full(512, 0.1, dtype=np.float32)
    following = np.full(512, -0.2, dtype=np.float32)
    results = [
        SimpleNamespace(audio=repeated, sample_rate=16000),
        SimpleNamespace(audio=repeated, sample_rate=16000),
        SimpleNamespace(audio=repeated, sample_rate=16000),
        SimpleNamespace(audio=repeated, sample_rate=16000),
        SimpleNamespace(audio=following, sample_rate=16000),
    ]
    packets = list(provider._stream_pcm_blocks(results))
    assert packets
    assert any(np.frombuffer(packet, dtype="<i2").mean() < 0 for packet in packets)


@pytest.mark.skipif(sys.platform == "darwin", reason="MacSay is supported on macOS")
def test_mac_say_refuses_on_non_mac():
    with pytest.raises(PlatformNotSupported):
        MacSayTTSProvider()
