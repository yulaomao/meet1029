import sys

import pytest

from app.exceptions import PlatformNotSupported
from app.providers.diarization import SortformerProvider
from app.providers.tts import Qwen3TTSMLXProvider


@pytest.mark.skipif(sys.platform == "darwin", reason="MLX is supported on macOS")
def test_optional_local_mlx_providers_refuse_on_non_mac():
    with pytest.raises(PlatformNotSupported):
        SortformerProvider()
    with pytest.raises(PlatformNotSupported):
        Qwen3TTSMLXProvider()
