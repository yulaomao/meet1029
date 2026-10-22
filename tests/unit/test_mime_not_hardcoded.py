"""MEETING-AUDIO: MediaRecorder mime is probed, not hardcoded."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORDER = ROOT / "app" / "web" / "static" / "js" / "recorder.js"


def test_mime_not_hardcoded():
    src = RECORDER.read_text(encoding="utf-8")
    assert "isTypeSupported" in src
    assert "MIME_CANDIDATES" in src
    # Must not be a single hardcoded mime assignment as the only path.
    assert "audio/webm;codecs=opus" in src
    assert "audio/mp4" in src
    # pickSupportedMime loops candidates rather than assigning one mime.
    assert "for (const mime of MIME_CANDIDATES)" in src or "for (const mime of" in src

def test_enroll_js_uses_pick_supported_mime():
    enroll = ROOT / "app" / "web" / "static" / "js" / "enroll.js"
    src = enroll.read_text(encoding="utf-8")
    assert "pickSupportedMime" in src
    assert "recorder.js" in src
    assert "MediaRecorder" in src
