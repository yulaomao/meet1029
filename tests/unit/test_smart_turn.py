from __future__ import annotations

import time
from types import SimpleNamespace

from app.realtime.smart_turn import SmartTurnResult
from app.services.speech_pipeline import SpeechPipeline


def test_soft_endpoint_reopens_same_turn_without_flushing(monkeypatch, app):
    class Analyzer:
        def predict_pcm16(self, _pcm):
            return SmartTurnResult(complete=False, probability=0.12, inference_ms=4.0)

    monkeypatch.setattr("app.services.speech_pipeline.get_smart_turn", lambda _app: Analyzer())
    emitted = []
    monkeypatch.setattr("app.services.speech_pipeline.broadcast_live", lambda *_args: emitted.append(_args))
    pipeline = SpeechPipeline(app)
    session = {
        "utt_pcm": bytearray(b"\0\0" * 16000),
        "utterance_id": 7,
        "turn_revision": 0,
        "utterance_start_ms": 100,
        "event_sequence": 0,
        "speech_active": True,
        "silence_ms": 450,
    }
    with app.app_context():
        pipeline._soft_endpoint(app, "meeting", session, end_ms=1200)
        assert session["pending_endpoint"]["complete"] is False
        assert session["speech_active"] is False
        assert pipeline._reopen_pending_endpoint(app, "meeting", session) is True
    assert session["turn_revision"] == 1
    assert session["pending_endpoint"] is None
    assert any(args[1] == "transcript_reopen" for args in emitted)


def test_completed_endpoint_uses_shorter_reopen_window(monkeypatch, app):
    class Analyzer:
        def predict_pcm16(self, _pcm):
            return SmartTurnResult(complete=True, probability=0.91, inference_ms=3.0)

    monkeypatch.setattr("app.services.speech_pipeline.get_smart_turn", lambda _app: Analyzer())
    monkeypatch.setattr("app.services.speech_pipeline.broadcast_live", lambda *_args: None)
    pipeline = SpeechPipeline(app)
    session = {"utt_pcm": bytearray(), "utterance_id": 1, "turn_revision": 0, "utterance_start_ms": 0, "speech_active": True}
    with app.app_context():
        before = time.monotonic()
        pipeline._soft_endpoint(app, "meeting", session, end_ms=200)
    remaining = session["pending_endpoint"]["deadline"] - before
    assert 0.5 < remaining < 1.0
