"""Launch Playwright with its managed Chromium or an installed system browser."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def launch_chromium(playwright, *, args: list[str] | None = None):
    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    candidates = [configured, shutil.which("chrome"), shutil.which("msedge")]
    if os.name == "nt":
        candidates.extend(
            [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ]
        )
    executable = next((str(Path(path)) for path in candidates if path and Path(path).is_file()), None)
    if executable:
        return playwright.chromium.launch(executable_path=executable, args=args or [])
    return playwright.chromium.launch(args=args or [])
