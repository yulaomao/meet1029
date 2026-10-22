import os

import pytest

from app.providers.llm import Qwen38LocalProvider


@pytest.mark.local_llm
def test_qwen38_local_gateway_contract():
    if os.environ.get("RUN_LOCAL_LLM") != "1":
        pytest.skip("RUN_LOCAL_LLM!=1")
    provider = Qwen38LocalProvider()
    text = provider.complete('Reply with JSON {"ok": true}', json_mode=True)
    assert "ok" in text.lower() or "{" in text
