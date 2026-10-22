#!/usr/bin/env python3
"""Optionally fetch a tiny public sample. Full AISHELL-4 / WenetSpeech / AliMeeting
are NOT downloaded. Synthetic data from generate_synthetic_meetings.py is enough.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "testdata" / "public"
MANIFEST = ROOT / "evals" / "manifests" / "public.json"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    info = {
        "aishell4": {"downloaded": False, "reason": "full corpus skipped by design"},
        "wenetspeech": {"downloaded": False, "reason": "full corpus skipped by design"},
        "alimeeting": {"downloaded": False, "reason": "full corpus skipped by design"},
        "musan": {"downloaded": False, "reason": "full corpus skipped by design"},
        "rirs": {"downloaded": False, "reason": "full corpus skipped by design"},
        "tiny_sample": {"downloaded": False},
    }
    try:
        url = "https://upload.wikimedia.org/wikipedia/commons/0/05/Silence.wav"
        dest = OUT / "silence.wav"
        urllib.request.urlretrieve(url, dest)
        info["tiny_sample"] = {"downloaded": True, "path": str(dest)}
    except Exception as exc:
        info["tiny_sample"] = {"downloaded": False, "error": str(exc)}
    MANIFEST.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
