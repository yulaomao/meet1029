"""FunASR adapters are lazy: unit tests never download or load model weights."""

from app.providers.funasr import FunASRFinalProvider, FunASRStreamingProvider, SenseVoiceRefiner
from app.providers.asr import FakeASRProvider
from app.providers.registry import ProviderRegistry
from app.providers.vad import EnergyVAD


def test_streaming_keeps_revisable_tail_and_flushes(monkeypatch):
    provider = FunASRStreamingProvider(chunk_ms=10)

    def infer(_pcm, *, is_final):
        provider._partial += "针尖球心补偿"
        provider._stable = provider._partial[:-20] if len(provider._partial) > 20 else ""
        if is_final:
            provider._final, provider._partial = provider._partial, ""

    monkeypatch.setattr(provider, "_infer", infer)
    provider.feed_audio(b"\x00\x00" * 160)
    assert provider.get_partial() == "针尖球心补偿"
    assert provider.get_stable_prefix() == ""
    provider.flush()
    assert provider.get_final() == "针尖球心补偿"


def test_stable_prefix_never_retracts_when_tail_threshold_is_crossed():
    provider = FunASRStreamingProvider(chunk_ms=10, stable_tail_chars=3)

    class Model:
        def __init__(self):
            self.texts = iter(("你好", "你好世界", "你好世界开始"))

        def generate(self, **_kwargs):
            return [{"text": next(self.texts)}]

    provider._model = Model()
    frame = b"\x00\x00" * 160
    stable = []
    for _ in range(3):
        provider.feed_audio(frame)
        stable.append(provider.get_stable_prefix())

    assert stable == ["", "你", "你好世"]
    assert all(current.startswith(previous) for previous, current in zip(stable, stable[1:]))


def test_streaming_session_separates_cache_but_reuses_model():
    provider = FunASRStreamingProvider()
    provider._model = object()

    first = provider.new_session()
    second = provider.new_session()
    first._cache["meeting"] = 1

    assert first is not second
    assert first._model is second._model is provider._model
    assert first._cache != second._cache


def test_streaming_inference_passes_hotwords_to_funasr():
    provider = FunASRStreamingProvider(hotwords="社保 基本工资")

    class Model:
        def __init__(self):
            self.kwargs = None

        def generate(self, **kwargs):
            self.kwargs = kwargs
            return [{"text": "社保基本工资"}]

    model = Model()
    provider._model = model
    provider._infer(b"\x00\x00" * 160, is_final=False)

    assert model.kwargs["hotword"] == "社保 基本工资"


def test_registry_selects_funasr_and_offline_vad():
    reg = ProviderRegistry(speech_backend="funasr", llm_backend="qwen38")
    assert isinstance(reg.asr, FunASRStreamingProvider)
    assert isinstance(reg.vad, EnergyVAD)


def test_registry_switches_local_streaming_and_refinement_models_from_environment(monkeypatch):
    streaming_model = r"E:\models\paraformer-online"
    sensevoice_model = r"E:\models\sensevoice"
    monkeypatch.setenv("FUNASR_STREAMING_MODEL", streaming_model)
    monkeypatch.setenv("SENSEVOICE_MODEL", sensevoice_model)
    monkeypatch.delenv("SENSEVOICE_REFINEMENT_ENABLED", raising=False)

    paraformer_only = ProviderRegistry(speech_backend="funasr")
    assert isinstance(paraformer_only.asr, FunASRStreamingProvider)
    assert paraformer_only.asr.model_id == streaming_model
    assert paraformer_only.asr._model is None
    assert paraformer_only.sensevoice is None

    monkeypatch.setenv("SENSEVOICE_REFINEMENT_ENABLED", "1")
    with_refinement = ProviderRegistry(speech_backend="funasr")
    assert isinstance(with_refinement.asr, FunASRStreamingProvider)
    assert with_refinement.asr.model_id == streaming_model
    assert isinstance(with_refinement.sensevoice, SenseVoiceRefiner)
    assert with_refinement.sensevoice.model_id == sensevoice_model
    assert with_refinement.sensevoice._model is None

    fake_backend = ProviderRegistry(speech_backend="fake")
    assert isinstance(fake_backend.asr, FakeASRProvider)
    assert fake_backend.sensevoice is None


def test_energy_vad_threshold_uses_local_configuration(monkeypatch):
    monkeypatch.setenv("VAD_ENERGY_THRESHOLD", "125")
    assert EnergyVAD().threshold == 125


def test_sensevoice_plain_text_removes_rich_metadata():
    assert SenseVoiceRefiner._plain_text("<|zh|><|NEUTRAL|><|Speech|><|woitn|>你好，会议开始") == "你好，会议开始"


def test_sensevoice_final_merges_only_short_adjacent_vad_regions():
    # A tiny VAD boundary must not turn one sentence into two fragments, while
    # a real pause remains a distinct, attributable meeting turn.
    assert FunASRFinalProvider._merge_vad_regions([(0, 1200), (1300, 2600), (4000, 5000)]) == [
        (0, 2600),
        (4000, 5000),
    ]
