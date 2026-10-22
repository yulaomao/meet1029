"""Local media-tool discovery that also works under macOS LaunchAgents."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _resolve_binary(name: str) -> str | None:
    """Find a Homebrew media binary even under a minimal LaunchAgent PATH."""
    configured = (os.environ.get(f"{name.upper()}_BIN") or "").strip()
    candidates = [configured] if configured else []
    candidates.extend(filter(None, [shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]))
    for raw in candidates:
        path = Path(raw)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def resolve_ffmpeg() -> str | None:
    """Return an executable local ffmpeg path without relying solely on PATH.

    LaunchAgents intentionally inherit a minimal PATH (usually /usr/bin:/bin),
    while Homebrew installs ffmpeg under /opt/homebrew/bin on Apple Silicon.
    """
    return _resolve_binary("ffmpeg")


def resolve_ffprobe() -> str | None:
    """Return an executable local ffprobe path for upload validation."""
    return _resolve_binary("ffprobe")
