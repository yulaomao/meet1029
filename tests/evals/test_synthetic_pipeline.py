from __future__ import annotations

import json
from pathlib import Path

from app.providers.final_asr import FakeFinalASRProvider
from app.providers.llm import FakeLLMProvider
from app.services.summary import parse_json_payload

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "testdata" / "manifest.json"


def test_synthetic_pipeline():
    if not MANIFEST.exists():
        from scripts.generate_synthetic_meetings import main

        main()
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert len(data["meetings"]) >= 3
    asr = FakeFinalASRProvider()
    llm = FakeLLMProvider()
    for item in data["meetings"]:
        wav = ROOT / item["wav"]
        assert wav.exists()
        result = asr.transcribe(str(wav))
        assert result["segments"]
        payload = parse_json_payload(llm.complete("summary json " + result["text"], json_mode=True))
        assert payload.get("summary")
        # App must not special-case private_release/expected.json
        src = (ROOT / "app").read_text if False else ""
    app_py = list((ROOT / "app").rglob("*.py"))
    blob = "\n".join(p.read_text(encoding="utf-8") for p in app_py)
    assert "private_release/expected.json" not in blob
    assert "private-001" not in blob
