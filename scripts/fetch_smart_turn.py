#!/usr/bin/env python3
"""Provision the Smart Turn v3.2 ONNX model for an offline deployment."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import urllib.request
from pathlib import Path

URL = "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx?download=true"
SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_file() and sha256(output) == SHA256:
        print(f"already present and verified: {output}")
        return
    temporary = output.with_suffix(output.suffix + ".part")
    request = urllib.request.Request(URL, headers={"User-Agent": "meeting-ai-local-provisioner"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as fh:
            shutil.copyfileobj(response, fh, length=1024 * 1024)
        actual = sha256(temporary)
        if actual != SHA256:
            raise RuntimeError(f"Smart Turn checksum mismatch: expected {SHA256}, got {actual}")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"downloaded: {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
