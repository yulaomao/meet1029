"""Speech worker tick(): PCM spool + chunk fallback, no schema migration."""

from __future__ import annotations

from pathlib import Path

from app.models import SpeakerProfile, TranscriptSegment, User
from app.realtime.framing import BYTES_PER_FRAME
from app.services.audio import AudioService
from app.services.meeting import MeetingService
from app.services.meeting_state import MeetingStateService
from app.realtime.echo import EchoFilter
from app.services.speech_pipeline import SpeechPipeline, tick
from app.services.transcript import TranscriptService
from app.web.extensions import socketio


def _start_meeting(app, name: str = "speech"):
    admin = User.query.filter_by(email="admin@local").first()
    m = MeetingService().create(admin, name=name)
    MeetingService().set_consent(m, True)
    MeetingService().start(m, admin, client_id="c1")
    return m, admin


def test_tick_ingested_chunk_creates_segment(app):
    with app.app_context():
        m, _admin = _start_meeting(app, "chunk-tick")
        store = app.extensions["audio_store"]
        AudioService(store).ingest_chunk(
            m.id, "c1", 0, b"RIFF" + b"\x00" * 64, mime="audio/wav", start_ms=0
        )
        stats = tick(app)
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        assert len(segs) >= 1
        assert segs[0].raw_text
        assert stats["chunks"] >= 1
        cursor = store.read_json(m.id, "speech_cursor.json")
        assert cursor is not None
        assert cursor["last_seq"] >= 0
        state = MeetingStateService().current(m.id)
        assert state.rolling_summary


def test_tick_pcm_file_finals_after_three_frames(app):
    with app.app_context():
        m, _admin = _start_meeting(app, "pcm-tick")
        store = app.extensions["audio_store"]
        pcm_path = store.meeting_dir(m.id) / "realtime.pcm"
        pcm_path.write_bytes(b"\x00" * (BYTES_PER_FRAME * 3))
        tick(app)
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        assert len(segs) >= 1
        assert segs[0].raw_text
        cursor = store.read_json(m.id, "speech_cursor.json")
        assert cursor["pcm_offset"] >= BYTES_PER_FRAME * 3


def test_worklet_is_classic_script():
    src = Path("app/web/static/js/audio_worklet.js").read_text(encoding="utf-8")
    assert "registerProcessor" in src
    assert "export " not in src
    assert "export{" not in src
    assert "export function" not in src
    assert "16000" in src
    assert "40" in src


def test_live_js_connects_worklet_to_destination():
    src = Path("app/web/static/js/live.js").read_text(encoding="utf-8")
    assert "createGain" in src
    assert "destination" in src
    assert "音频处理模块加载失败" in src
    assert "decodeAudioData" in src
    assert "playPcm16" in src
    assert "ensurePlaybackCtx" in src
    assert 'type = "highshelf"' in src
    assert "ttsToneFilter.gain.value = -3" in src
    assert "data-id" in src
    assert "live-summary" in src
    assert "asr_metrics" in src
    assert "transcriptScroller" in src
    assert "/api/meetings/" in src
    assert "transcript" in src
    assert "pollTranscript" in src or "/transcript" in src
    assert "setInterval(pollTranscript, 2000)" in src or "2000" in src


def test_live_html_has_summary_block():
    html = Path("app/web/templates/meetings/live.html").read_text(encoding="utf-8")
    assert 'id="live-summary"' in html
    assert "live-topic" in html
    assert "live-decisions" in html
    assert 'class="assistant-panel"' in html
    assert 'id="ai-state"' in html
    status = Path("app/web/templates/partials/status_bar.html").read_text(encoding="utf-8")
    assert 'id="ai-lag"' in status


def test_asr_queue_metrics_are_published_for_the_live_header(app, monkeypatch):
    events = []
    monkeypatch.setattr(
        "app.services.speech_pipeline.broadcast_live",
        lambda _app, event, meeting_id, payload: events.append((event, meeting_id, payload)),
    )
    cursor, session = {}, {}
    SpeechPipeline._publish_asr_metrics(
        app, "meeting-1", session, cursor, queue_lag_ms=740, processing_ms=63
    )
    assert events == [("asr_metrics", "meeting-1", {"queue_lag_ms": 740, "processing_ms": 63})]
    assert cursor["asr_queue_lag_ms"] == 740
    assert cursor["asr_processing_ms"] == 63


def test_interrupted_ai_turn_releases_llm_lock_without_waiting_for_gateway(app, monkeypatch):
    """A cancelled pre-token stream must not block the next spoken question."""
    import threading
    import time

    from app.services import speech_pipeline as speech_module

    class WaitingLLM:
        def __init__(self):
            self.started = threading.Event()

        def stream(self, prompt, *, model=None, cancel_event=None):
            self.started.set()
            while cancel_event is not None and not cancel_event.wait(0.01):
                pass
            return
            yield ""  # pragma: no cover - keeps this a generator for typing

    with app.app_context():
        meeting, _admin = _start_meeting(app, "cancel-stream-lock")
        waiting = WaitingLLM()
        providers = app.extensions["providers"]
        original = providers.llm
        providers.llm = waiting
        monkeypatch.setattr(speech_module, "broadcast_live", lambda *args, **kwargs: None)
        try:
            pipeline = SpeechPipeline(app)
            session = {"echo": EchoFilter()}
            assert pipeline._spawn_ai_turn(app, meeting.id, "成就精灵请回答", session)
            assert waiting.started.wait(1.0)
            assert pipeline._interrupt_ai_turn(app, meeting.id, session, reason="test")
            deadline = time.monotonic() + 1.0
            acquired = False
            while time.monotonic() < deadline:
                acquired = speech_module._llm_lock.acquire(blocking=False)
                if acquired:
                    break
                time.sleep(0.01)
            assert acquired, "cancelled stream retained the local LLM lock"
            speech_module._llm_lock.release()
        finally:
            providers.llm = original


def test_live_broadcast_loopback_emits(app, auth_admin):
    with app.app_context():
        m, _admin = _start_meeting(app, "broadcast")
        mid = m.id
    sio = socketio.test_client(app, namespace="/live", flask_test_client=auth_admin)
    sio.emit("join_meeting", {"meeting_id": mid}, namespace="/live")
    sio.get_received("/live")
    r = auth_admin.post(
        "/api/internal/live-broadcast",
        json={
            "event": "transcript_final",
            "meeting_id": mid,
            "payload": {"meeting_id": mid, "id": "seg1", "text": "hello", "speaker": "张工"},
        },
    )
    assert r.status_code == 200
    rec = sio.get_received("/live")
    names = [x["name"] for x in rec]
    assert "transcript_final" in names
    payload = next(x["args"][0] for x in rec if x["name"] == "transcript_final")
    assert payload["text"] == "hello"
    assert payload["speaker"] == "张工"
    sio.disconnect(namespace="/live")


def test_live_broadcast_non_loopback_403(client):
    r = client.post(
        "/api/internal/live-broadcast",
        json={"event": "transcript_final", "meeting_id": "x", "payload": {}},
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
    )
    assert r.status_code == 403


def test_store_pcm_also_writes_realtime_file(app):
    from app.web.socket_events import store_pcm

    with app.app_context():
        m, _admin = _start_meeting(app, "spool")
        store_pcm(m.id, b"abc")
        store_pcm(m.id, b"def")
        path = app.extensions["audio_store"].meeting_dir(m.id) / "realtime.pcm"
        assert path.read_bytes() == b"abcdef"
        assert bytes(app.extensions["pcm_buffers"][m.id]) == b"abcdef"

def test_broadcast_live_uses_trust_env_false():
    src = Path("app/services/speech_pipeline.py").read_text(encoding="utf-8")
    assert "trust_env=False" in src
    assert "timeout=2.0" in src


def test_broadcast_live_httpx_logs_status_on_502(app, monkeypatch, caplog):
    import logging

    from app.services.speech_pipeline import broadcast_live

    called = {}

    class FakeResp:
        status_code = 502

    def fake_post(*args, **kwargs):
        called["kwargs"] = kwargs
        called["url"] = args[0] if args else kwargs.get("url")
        return FakeResp()

    monkeypatch.setattr("httpx.post", fake_post)
    app.config["TESTING"] = False
    app.config["INTERNAL_LIVE_HOST"] = "127.0.0.1"
    app.config["INTERNAL_LIVE_PORT"] = 5050
    with caplog.at_level(logging.WARNING):
        broadcast_live(app, "transcript_final", "mid", {"text": "x"})
    assert called["kwargs"]["trust_env"] is False
    assert called["kwargs"]["timeout"] == 2.0
    assert "502" in caplog.text
    assert "5050" in str(called["url"])


def test_join_emits_existing_segments_but_heartbeat_does_not_replay(app, auth_admin):
    from app.services.transcript import TranscriptService
    from app.web.extensions import socketio

    with app.app_context():
        m, _admin = _start_meeting(app, "hb-catchup")
        mid = m.id
        TranscriptService().add_segment(mid, "hello from db", speaker_label="张工")
    sio = socketio.test_client(app, namespace="/live", flask_test_client=auth_admin)
    sio.emit("join_meeting", {"meeting_id": mid}, namespace="/live")
    joined = sio.get_received("/live")
    joined_names = [x["name"] for x in joined]
    assert "transcript_final" in joined_names
    sio.emit("heartbeat", {"meeting_id": mid}, namespace="/live")
    rec = sio.get_received("/live")
    names = [x["name"] for x in rec]
    assert "client_health" in names
    assert "transcript_final" not in names
    payload = next(x["args"][0] for x in joined if x["name"] == "transcript_final")
    assert payload["text"] == "hello from db"
    assert payload["speaker"] == "张工"
    sio.disconnect(namespace="/live")


def test_broadcast_live_disables_env_proxy():
    src = Path("app/services/speech_pipeline.py").read_text(encoding="utf-8")
    assert "trust_env=False" in src


def test_prepare_has_mic_device_picker():
    html = Path("app/web/templates/meetings/new.html").read_text(encoding="utf-8")
    assert 'id="mic-device"' in html
    assert "enumerateDevices" in html
    assert "meeting-mic-device-id" in html
    assert 'id="start-confirm"' in html


def test_live_js_uses_saved_mic_device():
    src = Path("app/web/static/js/live.js").read_text(encoding="utf-8")
    assert "meeting-mic-device-id" in src
    assert "deviceId" in src


def test_live_fake_asr_does_not_emit_canned_script(app):
    """Production (TESTING=False) must not loop FakeASR script on silence PCM."""
    with app.app_context():
        app.config["TESTING"] = False
        m, _admin = _start_meeting(app, "live-fake-skip")
        store = app.extensions["audio_store"]
        pcm_path = store.meeting_dir(m.id) / "realtime.pcm"
        pcm_path.write_bytes(b"\x00" * (BYTES_PER_FRAME * 30))
        stats = tick(app)
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        assert segs == []
        assert stats["segments"] == 0
        cursor = store.read_json(m.id, "speech_cursor.json")
        assert cursor["pcm_offset"] >= BYTES_PER_FRAME * 30



def test_local_vad_endpoint_flushes_only_speech(app):
    """FunASR receives speech only; local VAD silence finalizes the utterance."""
    from app.realtime.echo import EchoFilter
    from app.realtime.framing import BYTES_PER_FRAME
    from app.services.speech_pipeline import tick

    class StubASR:
        def __init__(self):
            self.fed = 0
            self._finals = []

        def start_session(self):
            return None

        def feed_audio(self, pcm16):
            self.fed += 1

        def get_partial(self):
            return ""

        def get_stable_prefix(self):
            return ""

        def get_final(self):
            if self._finals:
                return self._finals.pop(0)
            return ""

        def flush(self):
            self._finals.append("你好，成就精灵")

        def reset(self):
            return None

        def close(self):
            return None

    class SilentVAD:
        def __init__(self):
            self.frames = 0

        def is_speech(self, frame):
            self.frames += 1
            return self.frames == 1

    with app.app_context():
        m, _admin = _start_meeting(app, "server-vad-silence")
        stub = StubASR()
        app.extensions["speech_sessions"] = {
            m.id: {
                "asr": stub,
                "diarization": app.extensions["providers"].diarization,
                "echo": EchoFilter(),
            }
        }
        app.extensions["providers"].vad = SilentVAD()
        app.config["VAD_ENDPOINT_SILENCE_MS"] = 100
        store = app.extensions["audio_store"]
        pcm_path = store.meeting_dir(m.id) / "realtime.pcm"
        nframes = 2
        pcm_path.write_bytes(b"\x00" * (BYTES_PER_FRAME * nframes))
        tick(app)
        assert stub.fed == 1
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        assert any("你好" in (s.raw_text or "") for s in segs)



def test_tick_saves_cursor_when_rolling_summary_fails(app, monkeypatch):
    """A 27B 502 must not rewind pcm_offset and re-feed the same audio."""
    with app.app_context():
        m, _admin = _start_meeting(app, "cursor-on-llm-fail")
        store = app.extensions["audio_store"]
        pcm_path = store.meeting_dir(m.id) / "realtime.pcm"
        pcm_path.write_bytes(b"\x00" * (BYTES_PER_FRAME * 3))

        def boom(*_a, **_k):
            raise RuntimeError("HTTP 502")

        monkeypatch.setattr(app.extensions["providers"].llm, "complete", boom)
        tick(app)
        cursor = store.read_json(m.id, "speech_cursor.json")
        assert cursor["pcm_offset"] >= BYTES_PER_FRAME * 3
        assert float(cursor["last_summary_at"] or 0) > 0
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        assert len(segs) >= 1
        pcm_path.write_bytes(pcm_path.read_bytes() + b"\x00" * BYTES_PER_FRAME)
        tick(app)
        cursor2 = store.read_json(m.id, "speech_cursor.json")
        assert cursor2["pcm_offset"] >= cursor["pcm_offset"]


def test_on_final_skips_consecutive_duplicate(app):
    from app.realtime.echo import EchoFilter
    from app.services.speech_pipeline import SpeechPipeline

    with app.app_context():
        m, _admin = _start_meeting(app, "dup-final")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": app.extensions["providers"].diarization,
            "echo": EchoFilter(),
        }
        pipe = SpeechPipeline(app)
        first = pipe._on_final(app, m.id, "目前来", b"\x00" * BYTES_PER_FRAME, session, start_ms=0, end_ms=100)
        second = pipe._on_final(app, m.id, "目前来", b"\x00" * BYTES_PER_FRAME, session, start_ms=100, end_ms=200)
        third = pipe._on_final(app, m.id, "下一句", b"\x00" * BYTES_PER_FRAME, session, start_ms=200, end_ms=300)
        assert first is not None
        assert second is None
        assert third is not None
        texts = [s.raw_text for s in TranscriptSegment.query.filter_by(meeting_id=m.id).all()]
        assert texts.count("目前来") == 1
        assert "下一句" in texts

def test_live_js_paints_growing_partial_in_list():
    src = Path("app/web/static/js/live.js").read_text(encoding="utf-8")
    assert "transcript_partial" in src
    assert "partial-live" in src
    assert "latencyHint" in src
    assert "autoGainControl" in src


def test_tick_broadcasts_partial_before_final(app):
    from app.realtime.echo import EchoFilter

    class StreamingASR:
        uses_server_vad = True

        def __init__(self):
            self.fed = 0
            self._partial = ""
            self._finals = []

        def feed_audio(self, pcm16):
            self.fed += 1
            if self.fed == 1:
                self._partial = "针"
            elif self.fed == 2:
                self._partial = "针尖"
            else:
                self._partial = ""
                self._finals.append("针尖球心补偿")

        def get_partial(self):
            return self._partial

        def get_stable_prefix(self):
            return self._partial

        def flush(self):
            return None

        def get_final(self):
            if self._finals:
                return self._finals.pop(0)
            return ""

        def reset(self):
            return None

        def close(self):
            return None

    with app.app_context():
        m, _admin = _start_meeting(app, "partial-before-eou")
        stub = StreamingASR()
        app.extensions["speech_sessions"] = {
            m.id: {
                "asr": stub,
                "diarization": app.extensions["providers"].diarization,
                "echo": EchoFilter(),
            }
        }
        store = app.extensions["audio_store"]
        pcm_path = store.meeting_dir(m.id) / "realtime.pcm"
        pcm_path.write_bytes(b"\x00" * BYTES_PER_FRAME)
        tick(app)
        assert stub.fed == 1
        assert TranscriptSegment.query.filter_by(meeting_id=m.id).count() == 0
        pcm_path.write_bytes(pcm_path.read_bytes() + b"\x00" * BYTES_PER_FRAME)
        tick(app)
        assert stub.fed == 2
        assert TranscriptSegment.query.filter_by(meeting_id=m.id).count() == 0
        pcm_path.write_bytes(pcm_path.read_bytes() + b"\x00" * BYTES_PER_FRAME)
        tick(app)
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        assert any("针尖球心补偿" in (s.raw_text or "") for s in segs)


def test_live_js_replaces_caption_with_same_id():
    src = Path("app/web/static/js/live.js").read_text(encoding="utf-8")
    assert "article[data-id=" in src
    assert "if (id && list.querySelector('article[data-id=\"' + id + '\"]')) return;" not in src
    assert "p.textContent = text" in src or 'p.textContent = text' in src


def test_on_final_stores_raw_and_offline_punctuation(app, monkeypatch):
    from app.realtime.echo import EchoFilter
    from app.realtime.framing import BYTES_PER_FRAME
    from app.services.speech_pipeline import SpeechPipeline

    monkeypatch.setattr(SpeechPipeline, "_spawn_ai_turn", lambda *a, **k: None)
    monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *a, **k: None)
    with app.app_context():
        m, _admin = _start_meeting(app, "asr-correct-local")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": app.extensions["providers"].diarization,
            "echo": EchoFilter(),
        }
        pipe = SpeechPipeline(app)
        seg = pipe._on_final(
            app, m.id, "陈酒精灵总结一下", b"\x00" * BYTES_PER_FRAME, session, start_ms=0, end_ms=100
        )
        assert seg is not None
        assert seg.raw_text == "陈酒精灵总结一下"
        assert seg.punctuated_text == "陈酒精灵总结一下。"
        assert seg.display_text == "陈酒精灵总结一下。"
        assert seg.corrected_text is None
        assert session["last_final_text"] == "陈酒精灵总结一下"


def test_on_final_prefix_overlap_never_mutates_raw(app, monkeypatch):
    from app.realtime.echo import EchoFilter
    from app.realtime.framing import BYTES_PER_FRAME
    from app.services.speech_pipeline import SpeechPipeline

    monkeypatch.setattr(SpeechPipeline, "_spawn_ai_turn", lambda *a, **k: None)
    monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *a, **k: None)
    with app.app_context():
        m, _admin = _start_meeting(app, "asr-correct-prefix")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": app.extensions["providers"].diarization,
            "echo": EchoFilter(),
        }
        pipe = SpeechPipeline(app)
        first = pipe._on_final(app, m.id, "陈酒", b"\x00" * BYTES_PER_FRAME, session, start_ms=0, end_ms=80)
        second = pipe._on_final(
            app, m.id, "陈酒精灵请回答", b"\x00" * BYTES_PER_FRAME, session, start_ms=0, end_ms=200
        )
        assert first is not None
        assert second is None
        assert first.raw_text == "陈酒"



def test_on_final_sticky_speaker_despite_cycling_diarization(app, monkeypatch):
    from app.realtime.echo import EchoFilter
    from app.realtime.framing import BYTES_PER_FRAME
    from app.services.speech_pipeline import SpeechPipeline

    monkeypatch.setattr(SpeechPipeline, "_spawn_ai_turn", lambda *a, **k: None)
    monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *a, **k: None)

    class CycleDia:
        def __init__(self):
            self.n = 0

        def current_speakers(self):
            self.n += 1
            idx = self.n % 2
            return [{"speaker": f"Speaker {idx + 1}", "cluster_id": str(idx), "probability": 0.9}]

    with app.app_context():
        m, _admin = _start_meeting(app, "sticky-human")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": CycleDia(),
            "echo": EchoFilter(),
        }
        pipe = SpeechPipeline(app)
        texts = [
            "现在在进行最后一次测试",
            "针对于当前我们所出现的这个问题你之前做了多次的整改",
            "但是始终没有达到我们所期望的目标。",
            "你可以看整个会议记录啊",
            "你可以使用tts进行语音回复吗",
        ]
        segs = []
        for i, t in enumerate(texts):
            segs.append(
                pipe._on_final(
                    app, m.id, t, b"\x00" * BYTES_PER_FRAME, session, start_ms=i * 2000, end_ms=i * 2000 + 1800
                )
            )
        labels = [s.speaker_label for s in segs if s is not None]
        assert labels
        assert set(labels) == {"Speaker 1"}


def test_prefix_overlap_collapses_stutter_extend():
    from app.services.speech_pipeline import SpeechPipeline

    assert SpeechPipeline._prefix_overlap("你可以你可以可可以看一以", "你可以看整个会议记录啊") is True
    assert SpeechPipeline._prefix_overlap("陈酒", "陈酒精灵请回答") is True
    assert SpeechPipeline._prefix_overlap("现在在进行最后一次测试", "你可以看整个会议记录啊") is False


def test_live_js_plays_tts_via_audiocontext():
    src = Path("app/web/static/js/live.js").read_text(encoding="utf-8")
    assert "socket.on(\"tts_audio\"" in src or "tts_audio" in src
    assert "decodeAudioData" in src
    assert "createBufferSource" in src
    assert "playPcm16" in src
    assert "16000" in src
    assert "stopTtsPlayback" in src
    assert "tts_stop" in src
    assert "tts_hold" in src
    assert "tts_resume" in src
    assert "utterance_id" in src
    # The player is created lazily, after ai_status may already have advanced
    # the server generation. Keep first-turn PCM from being discarded as stale.
    assert 'kind: "generation", generation: activeAiGeneration' in src
    assert "await ensurePlaybackCtx()" in src


def test_ai_barge_in_cancels_active_turn(app):
    with app.app_context():
        m, _admin = _start_meeting(app, "barge-in")
        pipe = SpeechPipeline(app)
        session = {
            "echo": EchoFilter(),
            "ai_cancel": __import__("threading").Event(),
            "ai_turn_active": True,
            "ai_turn_id": 7,
            "barge_speech_ms": 500,
        }
        session["echo"].on_tts("这是一段将被打断的回答")
        assert pipe._interrupt_ai_turn(app, m.id, session, reason="voice_activity") is True
        assert session["ai_cancel"].is_set()
        assert session["ai_turn_active"] is False
        assert session["echo"].ai_speaking is False


def test_manual_stop_marker_is_consumed_by_worker(app):
    with app.app_context():
        m, _admin = _start_meeting(app, "manual-stop")
        pipe = SpeechPipeline(app)
        session = {
            "echo": EchoFilter(),
            "ai_cancel": __import__("threading").Event(),
            "ai_turn_active": True,
            "ai_turn_id": 3,
            "barge_speech_ms": 0,
        }
        app.extensions["audio_store"].write_json(m.id, "ai_control.json", {"stop_turn_id": 3})
        pipe._consume_ai_control(app, m.id, session)
        assert session["ai_cancel"].is_set()


def test_confirmed_voiceprint_cluster_keeps_medium_confidence_turns(app, monkeypatch):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        profile = SpeakerProfile(
            user_id=admin.id,
            display_name="张工",
            embedding=[1.0] + [0.0] * 63,
        )
        from app.web.extensions import db

        db.session.add(profile)
        db.session.commit()
        app.config["SPEAKER_HIGH_CONFIDENCE"] = 0.68
        app.config["SPEAKER_MID_CONFIDENCE"] = 0.55
        vectors = iter([[0.70, (1 - 0.70**2) ** 0.5] + [0.0] * 62, [0.62, (1 - 0.62**2) ** 0.5] + [0.0] * 62])
        monkeypatch.setattr(app.extensions["providers"].embedding, "embed_audio", lambda _pcm: next(vectors))
        pipe = SpeechPipeline(app)
        session = {}
        first = pipe._label_speaker(
            app, b"voice-a", [{"speaker": "Speaker 1", "cluster_id": "0"}], session, duration_ms=2200
        )
        second = pipe._label_speaker(
            app, b"voice-b", [{"speaker": "Speaker 1", "cluster_id": "0"}], session, duration_ms=2200
        )
        assert first[0] == second[0] == "张工"
        assert first[1] == second[1] == admin.id


def test_live_speaker_cluster_threshold_is_configurable(app, monkeypatch):
    with app.app_context():
        app.config["SPEAKER_ID_MIN_MS"] = 1000
        app.config["SPEAKER_CLUSTER_MATCH_THRESHOLD"] = 0.35
        vectors = iter([[1.0, 0.0], [0.4, (1 - 0.4**2) ** 0.5]])
        monkeypatch.setattr(app.extensions["providers"].embedding, "embed_audio", lambda _pcm: next(vectors))
        pipe = SpeechPipeline(app)
        session = {}

        first = pipe._label_speaker(
            app, b"voice-a", [], session, duration_ms=2200
        )
        second = pipe._label_speaker(
            app, b"voice-b", [], session, duration_ms=2200
        )

        assert first[0] == second[0] == "Speaker 1"
        assert first[2] == second[2] == "0"


def test_live_final_does_not_wait_for_campp_embedding(app, monkeypatch):
    from threading import Event

    from app.providers.embedding import CampPlusSpeakerEmbeddingProvider
    from app.services.speaker import StickySpeakerAssigner

    entered = Event()
    release = Event()
    emitted = []

    def slow_embedding(_pcm):
        entered.set()
        assert release.wait(timeout=2)
        return [0.0, 1.0]

    with app.app_context():
        meeting, _admin = _start_meeting(app, "async-campp-final")
        app.config["SPEAKER_ID_MIN_MS"] = 1000
        app.config["SPEAKER_CLUSTER_MATCH_THRESHOLD"] = 0.45
        provider = CampPlusSpeakerEmbeddingProvider(model="unused-test-checkpoint")
        provider.embed_audio = slow_embedding
        app.extensions["providers"].embedding = provider
        pipe = SpeechPipeline(app)
        monkeypatch.setattr(SpeechPipeline, "_spawn_ai_turn", lambda *args, **kwargs: None)
        monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *args, **kwargs: None)
        monkeypatch.setattr(SpeechPipeline, "_queue_sensevoice_refinement", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "app.services.speech_pipeline.broadcast_live",
            lambda _app, event, _meeting_id, payload: emitted.append((event, payload)),
        )
        session = pipe._session(app, meeting.id)
        assigner = StickySpeakerAssigner(match_threshold=0.45)
        assigner.assign(duration_ms=2000, embedding=[1.0, 0.0])
        session["speaker_assigner"] = assigner

        segment = pipe._on_final(
            app,
            meeting.id,
            "今天我们确认测试结果",
            b"\x00" * BYTES_PER_FRAME,
            session,
            start_ms=0,
            end_ms=2000,
        )

        assert segment is not None
        assert entered.wait(timeout=1)
        assert segment.speaker_label == "Speaker 1"
        release.set()
        executor = session.get("speaker_executor")
        assert executor is not None
        executor.shutdown(wait=True)
        session["speaker_executor"] = None

        from app.web.extensions import db

        db.session.refresh(segment)
        assert segment.speaker_label == "Speaker 2"
        assert segment.raw_text == "今天我们确认测试结果"
        speaker_updates = [payload for event, payload in emitted if event == "transcript_final" and payload.get("speaker_update")]
        assert speaker_updates[-1]["speaker"] == "Speaker 2"


def test_context_critical_short_wake_uses_voiceprint_before_prompt(app, monkeypatch):
    """A short wake phrase may be the only speaker evidence Qwen receives."""
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        from app.web.extensions import db

        db.session.add(SpeakerProfile(user_id=admin.id, display_name="张工", embedding=[1.0] + [0.0] * 63))
        db.session.commit()
        app.config["SPEAKER_ID_MIN_MS"] = 1500
        app.config["AI_CONTEXT_SPEAKER_ID_MIN_MS"] = 700
        monkeypatch.setattr(
            app.extensions["providers"].embedding,
            "embed_audio",
            lambda _pcm: [1.0] + [0.0] * 63,
        )
        label, user_id, _cluster, confidence = SpeechPipeline()._label_speaker(
            app,
            b"short-wake",
            [{"speaker": "Speaker 1", "cluster_id": "0"}],
            {},
            duration_ms=800,
            context_critical=True,
        )
        assert (label, user_id, confidence) == ("张工", admin.id, 1.0)


def test_confirmed_voiceprint_backfills_prior_anonymous_cluster_turns(app):
    with app.app_context():
        m, admin = _start_meeting(app, "voiceprint-backfill")
        prior = TranscriptService().add_segment(
            m.id,
            "先说的一句。",
            speaker_label="Speaker 1",
            speaker_cluster_id="0",
        )
        SpeechPipeline._backfill_cluster_identity(
            app,
            m.id,
            "0",
            label="张工",
            user_id=admin.id,
            confidence=0.7,
        )
        from app.web.extensions import db

        db.session.refresh(prior)
        assert prior.speaker_label == "张工"
        assert prior.speaker_user_id == admin.id


def test_manual_stop_can_end_idle_continuous_dialogue(app):
    with app.app_context():
        m, _admin = _start_meeting(app, "manual-stop-dialogue")
        pipe = SpeechPipeline(app)
        session = {
            "echo": EchoFilter(),
            "ai_cancel": __import__("threading").Event(),
            "ai_turn_active": False,
            "ai_turn_id": 3,
            "dialogue_active": True,
            "dialogue_last_activity": 123.0,
        }
        app.extensions["audio_store"].write_json(
            m.id,
            "ai_control.json",
            {"stop_turn_id": 3, "end_dialogue": True, "requested_at": 1.0},
        )
        pipe._consume_ai_control(app, m.id, session)
        assert session["dialogue_active"] is False
        assert session["dialogue_last_activity"] == 0.0


def test_tts_sentence_buffer_is_short_and_finalizable():
    assert SpeechPipeline._take_tts_sentence("这里是第一句。后续内容") == ("这里是第一句。", "后续内容")
    # A queued second sentence must not make the first one wait for the end of
    # the response.  This is the boundary used by the streaming TTS queue.
    assert SpeechPipeline._take_tts_sentence("第一句已经完整。第二句还在队列中。") == (
        "第一句已经完整。", "第二句还在队列中。"
    )
    assert SpeechPipeline._take_tts_sentence("没有标点的短句", final=True) == ("没有标点的短句", "")


def test_ai_starts_tts_before_llm_stream_finishes(app, monkeypatch):
    """Regression: audio is fed from streamed sentence batches, not full text."""
    import threading

    class PausedLLM:
        def __init__(self):
            self.waiting = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def stream(self, _prompt, *, model=None, cancel_event=None):
            yield "第一句已经完整。"
            yield "第二句已经完整。"
            yield "第三句已经完整。"
            self.waiting.set()
            self.release.wait(2.0)
            self.finished.set()
            yield "后续文本仍在生成。"

    class RecordingTTS:
        SAMPLE_RATE = 16000
        STREAMS_PCM16 = True

        def __init__(self):
            self.started = threading.Event()

        def synthesize(self, _text):
            return b"\x00\x00" * 512

        def stream(self, _text):
            self.started.set()
            yield b"\x00\x00" * 512

    with app.app_context():
        meeting, _admin = _start_meeting(app, "streaming-tts-before-llm-end")
        providers = app.extensions["providers"]
        original_llm, original_tts = providers.llm, providers.tts
        llm, tts = PausedLLM(), RecordingTTS()
        providers.llm, providers.tts = llm, tts
        monkeypatch.setattr("app.services.speech_pipeline.broadcast_live", lambda *args, **kwargs: None)
        try:
            pipeline = SpeechPipeline(app)
            assert pipeline._spawn_ai_turn(app, meeting.id, "成就精灵请回答", {"echo": EchoFilter()})
            assert llm.waiting.wait(1.0)
            assert tts.started.wait(1.0), "TTS waited for the LLM EndOfResponse"
            assert not llm.finished.is_set()
            llm.release.set()
        finally:
            providers.llm, providers.tts = original_llm, original_tts


def test_tts_units_keep_substantial_natural_context():
    text = "第一点是明确目标并收集数据。第二点是比较两套方案的风险和成本，最后确定负责人与时间表。"
    units = SpeechPipeline._split_tts_units(text, max_chars=32)
    assert "".join(units) == text
    assert all(len(unit) >= 16 for unit in units)


def test_barge_in_requires_meaningful_final_speech(app):
    meaningful = SpeechPipeline._is_meaningful_barge_in
    # Natural backchannels must not cancel the assistant just because VAD heard them.
    assert not meaningful(app, "嗯")
    assert not meaningful(app, "啊呀", duration_ms=1200)
    assert not meaningful(app, "好的", duration_ms=1200)
    assert not meaningful(app, "我觉得", duration_ms=300)
    assert meaningful(app, "我觉得这里需要修改", duration_ms=900)
    # Concise but explicit correction/question commands retain a quick path.
    assert meaningful(app, "请说慢点", duration_ms=750)


def test_dialogue_turn_policy_separates_backchannels_hold_and_handoff():
    from app.realtime.dialogue import DialogueTurnPolicy

    policy = DialogueTurnPolicy()
    # Listener feedback must neither cancel TTS nor create an accidental turn.
    for text in ("嗯", "嗯啊", "好的", "啊好的"):
        assert policy.decide_active_turn(text, duration_ms=1300).action == "ignore"
    # A terse control request takes the floor, but waits for the actual question.
    assert policy.decide_active_turn("等一下", duration_ms=600).action == "hold"
    assert policy.decide_active_turn("继续", duration_ms=600).action == "handoff"
    # A substantive utterance both stops the old answer and becomes the next turn.
    assert policy.decide_active_turn("我想问一下这个方案风险怎么处理", duration_ms=1300).action == "handoff"
    # Once the immediate reply window has elapsed, ordinary meeting discussion
    # cannot wake the assistant without an intent/question cue.
    assert not policy.should_accept_follow_up(
        "这个项目下周由产品团队继续推进", duration_ms=1300, plain_speech_allowed=False
    )
    assert policy.should_accept_follow_up(
        "这个项目下周如何推进", duration_ms=1000, plain_speech_allowed=False
    )


def test_final_meaningful_speech_interrupts_but_backchannel_does_not(app, monkeypatch):
    monkeypatch.setattr(SpeechPipeline, "_spawn_ai_turn", lambda *a, **k: False)
    monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *a, **k: None)
    with app.app_context():
        m, _admin = _start_meeting(app, "semantic-barge-in")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": app.extensions["providers"].diarization,
            "echo": EchoFilter(),
            "ai_cancel": __import__("threading").Event(),
            "ai_turn_active": True,
            "ai_turn_id": 9,
            "barge_speech_ms": 0,
        }
        pipe = SpeechPipeline(app)
        pipe._on_final(app, m.id, "嗯啊", b"\x00" * BYTES_PER_FRAME, session, start_ms=0, end_ms=1100)
        assert not session["ai_cancel"].is_set()
        pipe._on_final(app, m.id, "我觉得这里需要修改", b"\x00" * BYTES_PER_FRAME, session, start_ms=1200, end_ms=2300)
        assert session["ai_cancel"].is_set()


def test_active_turn_handoff_starts_exactly_one_follow_up(app, monkeypatch):
    from app.realtime.echo import EchoFilter

    calls = []

    def capture_spawn(_self, _app, _meeting_id, text, _session, **kwargs):
        calls.append((text, kwargs))
        return True

    monkeypatch.setattr(SpeechPipeline, "_spawn_ai_turn", capture_spawn)
    monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *a, **k: None)
    with app.app_context():
        m, _admin = _start_meeting(app, "handoff-once")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": app.extensions["providers"].diarization,
            "echo": EchoFilter(),
            "ai_cancel": __import__("threading").Event(),
            "ai_turn_active": True,
            "ai_turn_id": 11,
            "dialogue_active": True,
        }
        SpeechPipeline(app)._on_final(
            app, m.id, "我想问一下这个方案风险怎么处理", b"\x00" * BYTES_PER_FRAME,
            session, start_ms=0, end_ms=1300,
        )
    assert session["ai_cancel"].is_set()
    assert len(calls) == 1
    assert calls[0][1].get("follow_up") is True


def test_ai_correction_only_allows_one_small_exact_replacement():
    source = "目前来看实施性目以及各种问题都还可以"
    assert SpeechPipeline._apply_safe_replacement(
        source, {"from": "实施性目", "to": "实施性目标"}
    ) == "目前来看实施性目标以及各种问题都还可以"
    assert SpeechPipeline._apply_safe_replacement("东西工准", {"from": "工准", "to": "归准"}) is None
    assert SpeechPipeline._apply_safe_replacement(
        "这我我就我我一直希望这个都半能吵的来一直在你一直在创作",
        {"from": "我我一直希望这个都半能吵的来", "to": "我一直希望这个都能吵得起来"},
    ) is None


def test_contextual_correction_allows_faithful_multi_edit_repair(app):
    source = "啊下次测试AA的那个绘画功能主要是测试困嗯三困三的TTS的这个绘画功能但是目前它好像没有开始录音"
    repaired = "下次测试Qwen3的那个绘画功能，主要是测试Qwen3的TTS功能。但是目前它好像没有开始录音。"
    assert SpeechPipeline._valid_contextual_correction(app, source, repaired)
    assert not SpeechPipeline._valid_contextual_correction(
        app,
        source,
        "建议先完成录音链路排查，并由测试负责人确认发布条件。",
    )


def test_contextual_correction_normalizes_duplicate_terminal_punctuation():
    clean = SpeechPipeline._normalize_correction_punctuation
    assert clean("已经开始录音。。为什么没有开始？？") == "已经开始录音。为什么没有开始？"
    assert clean("目前，，，测试。：：正常！！") == "目前，测试。正常！"


def test_two_stage_barge_hold_and_backchannel_resume(app, monkeypatch):
    from app.realtime.echo import EchoFilter

    events = []
    monkeypatch.setattr(
        "app.services.speech_pipeline.broadcast_live",
        lambda _app, event, _meeting_id, payload: events.append((event, payload)),
    )
    with app.app_context():
        m, _admin = _start_meeting(app, "barge-hold-resume")
        # Meeting setup emits its own status notification asynchronously; this
        # assertion concerns only the hold/resume pair below.
        events.clear()
        pipe = SpeechPipeline(app)
        session = {
            "echo": EchoFilter(),
            "ai_cancel": __import__("threading").Event(),
            "ai_turn_active": True,
            "ai_turn_id": 4,
            "dialogue_active": True,
        }
        assert pipe._hold_ai_playback(app, m.id, session)
        assert session["barge_pending"] is True
        assert pipe._resume_ai_playback(app, m.id, session, reason="backchannel")
    assert [event for event, _payload in events] == ["tts_hold", "ai_status", "tts_resume", "ai_status"]


def test_echo_final_releases_a_held_tts_turn(app, monkeypatch):
    """Assistant playback leaking into the mic must never leave TTS paused."""
    events = []
    monkeypatch.setattr(
        "app.services.speech_pipeline.broadcast_live",
        lambda _app, event, _meeting_id, payload: events.append((event, payload)),
    )
    with app.app_context():
        pipe = SpeechPipeline(app)
        echo = EchoFilter()
        echo.on_tts("这是正在播放的完整回答，用于验证回声过滤能够恢复播放。")
        session = {
            "echo": echo,
            "ai_turn_active": True,
            "barge_pending": True,
            "ai_turn_id": 7,
            "ai_generation": 7,
        }
        assert pipe._on_final(app, "meeting", "这是正在播放的完整回答", b"", session) is None
    assert session["barge_pending"] is False
    assert [event for event, _ in events] == ["tts_resume", "ai_status"]


def test_continuous_dialogue_does_not_expire_without_explicit_stop(app):
    import time

    pipe = SpeechPipeline(app)
    session = {"dialogue_active": True, "dialogue_last_activity": time.monotonic() - 3600}
    assert pipe._dialogue_accepts_follow_up(app, session, "请继续解释刚才的方案", duration_ms=1200)
    assert session["dialogue_active"] is True


def test_nearby_final_replay_is_dropped_but_repeated_request_is_kept(app, monkeypatch):
    from app.realtime.echo import EchoFilter

    monkeypatch.setattr(SpeechPipeline, "_spawn_ai_turn", lambda *a, **k: False)
    monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *a, **k: None)
    with app.app_context():
        m, _admin = _start_meeting(app, "nearby-final-dedup")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": app.extensions["providers"].diarization,
            "echo": EchoFilter(),
        }
        pipe = SpeechPipeline(app)
        first = pipe._on_final(app, m.id, "成就精灵总结一下", b"\x00" * BYTES_PER_FRAME, session, start_ms=1000, end_ms=2400)
        replay = pipe._on_final(app, m.id, "成就精灵总结一下", b"\x00" * BYTES_PER_FRAME, session, start_ms=1500, end_ms=2600)
        repeated = pipe._on_final(app, m.id, "成就精灵总结一下", b"\x00" * BYTES_PER_FRAME, session, start_ms=7000, end_ms=8400)
    assert first is not None
    assert replay is None
    assert repeated is not None


def test_complete_slow_short_reply_uses_bounded_measured_speedup():
    rate = SpeechPipeline._short_reply_playback_rate(
        "收到，测试语速调整，这次回复字数较少。",
        5.86,
        base_rate=1.30,
        max_rate=1.60,
        short_reply_chars=28,
        target_chars_per_second=5.0,
    )
    assert rate == 1.60
    # A normal longer reply retains the user's configured playback rate.
    assert SpeechPipeline._short_reply_playback_rate(
        "这是一段较长的正常回答，用于验证不会因为单个短句规则而整体加速。",
        8.0,
        base_rate=1.30,
        max_rate=1.60,
        short_reply_chars=28,
        target_chars_per_second=5.0,
    ) == 1.30
    # Very short material uses the same measured target rather than a fixed,
    # arbitrary multiplier.
    assert SpeechPipeline._short_reply_playback_rate(
        "可以继续。",
        1.2,
        base_rate=1.30,
        max_rate=1.60,
        short_reply_chars=28,
        target_chars_per_second=5.0,
    ) == 1.50


def test_spoken_answer_is_short_by_default_and_long_for_explicit_detail(app):
    # Keep this larger than both configured limits.  The production defaults
    # intentionally allow 1,000 / 2,000 Chinese characters for voice output.
    answer = "第一句说明现状。第二句补充背景。第三句提出下一步安排。" * 80
    short = SpeechPipeline._spoken_answer(app, "现在情况怎么样", answer)
    detailed = SpeechPipeline._spoken_answer(app, "请给出详细解决方案", answer)
    assert len(short) <= app.config["AI_TTS_DEFAULT_MAX_CHARS"]
    assert len(detailed) <= app.config["AI_TTS_DETAILED_MAX_CHARS"]
    assert len(detailed) > len(short)


def test_local_disfluency_cleanup_is_narrow_and_auditable():
    clean = SpeechPipeline._clean_local_disfluencies
    assert clean("我我我觉得这个这个方案可以") == "我觉得这个方案可以"
    assert clean("你可以你可以看一下") == "你可以看一下"
    # Backchannels and ordinary reduplication are not ASR errors by themselves.
    assert clean("嗯嗯") == "嗯嗯"
    assert clean("看看这个") == "看看这个"


def test_continuous_dialogue_accepts_interruption_until_spoken_stop(app):
    from app.realtime.wake import WakeDetector

    pipe = SpeechPipeline(app)
    session = {"dialogue_active": True, "dialogue_last_activity": __import__("time").monotonic()}
    assert pipe._dialogue_accepts_follow_up(app, session, "我想补充一下测试方法")
    assert not pipe._dialogue_accepts_follow_up(app, session, "嗯嗯")
    wake = WakeDetector()
    assert wake.is_stop_command("成就精灵停止回复")


def test_garbled_spoken_stop_cannot_be_routed_as_a_new_question(app, monkeypatch):
    """Control detection runs before delayed SenseVoice correction/wake routing."""
    calls = []
    monkeypatch.setattr(
        SpeechPipeline,
        "_spawn_ai_turn",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)) or True,
    )
    monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *a, **k: None)
    with app.app_context():
        m, _admin = _start_meeting(app, "garbled-stop")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": app.extensions["providers"].diarization,
            "echo": EchoFilter(),
            "ai_cancel": __import__("threading").Event(),
            "ai_turn_active": True,
            "ai_turn_id": 4,
            "dialogue_active": True,
        }
        segment = SpeechPipeline(app)._on_final(
            app,
            m.id,
            "成就有精灵挺止回复的",
            b"\x00" * BYTES_PER_FRAME,
            session,
            start_ms=0,
            end_ms=1000,
        )
    assert segment is not None
    assert segment.display_text == "成就精灵停止回复。"
    assert session["ai_cancel"].is_set()
    assert session["dialogue_active"] is False
    assert calls == []


def test_assistant_turn_prepares_corrected_current_context_before_prompt(app, monkeypatch):
    class OneLineCorrector:
        def complete(self, prompt, *, json_mode=False, model=None):
            assert json_mode is False
            assert "这一句ASR" in prompt
            return "给到成就精灵的当前会话是未修正过后的吗"

    events = []
    monkeypatch.setattr(
        "app.services.speech_pipeline.broadcast_live",
        lambda _app, event, _meeting_id, payload: events.append((event, payload)),
    )
    with app.app_context():
        m, _admin = _start_meeting(app, "prepared-ai-context")
        original = "给到成就精灵的当前绘画是未修正过后的吗"
        segment = TranscriptService().add_segment(m.id, original, punctuated_text=original + "？")
        pipe = SpeechPipeline(app)
        result = pipe._prepare_ai_context_text(
            app,
            m.id,
            segment.id,
            segment.display_text,
            {"recent_captions": [segment.display_text]},
            OneLineCorrector(),
        )
        from app.web.extensions import db

        updated = db.session.get(TranscriptSegment, segment.id)
    assert "当前会话" in result
    assert updated.corrected_text == result
    assert any(event == "transcript_final" for event, _payload in events)


def test_on_final_routes_active_dialogue_to_follow_up(app, monkeypatch):
    from app.realtime.echo import EchoFilter

    calls = []

    def capture_spawn(_self, _app, _meeting_id, text, _session, **kwargs):
        calls.append((text, kwargs))
        return True

    monkeypatch.setattr(SpeechPipeline, "_spawn_ai_turn", capture_spawn)
    monkeypatch.setattr(SpeechPipeline, "_queue_correction", lambda *a, **k: None)
    with app.app_context():
        m, _admin = _start_meeting(app, "dialogue-follow-up")
        session = {
            "asr": app.extensions["providers"].asr,
            "diarization": app.extensions["providers"].diarization,
            "echo": EchoFilter(),
            "dialogue_active": True,
            "dialogue_last_activity": __import__("time").monotonic(),
        }
        SpeechPipeline(app)._on_final(
            app, m.id, "我想补充一下测试方法", b"\x00" * BYTES_PER_FRAME, session, start_ms=0, end_ms=1200
        )
    assert calls and calls[-1][1].get("follow_up") is True


def test_live_partial_keeps_asr_prefix_when_online_punctuation_returns_delta(monkeypatch):
    from types import SimpleNamespace

    import app.services.speech_pipeline as speech_pipeline

    class StreamingASR:
        def __init__(self):
            self.values = iter(("嗯", "嗯就是"))

        def get_stable_prefix(self):
            return next(self.values)

        def get_partial(self):
            return ""

        def get_final(self):
            return None

    class DeltaPunctuation:
        def __init__(self):
            self.values = iter(("嗯", "就是"))

        def online(self, _text):
            return next(self.values)

    events = []
    monkeypatch.setattr(
        speech_pipeline,
        "broadcast_live",
        lambda _app, event, _meeting_id, payload: events.append((event, payload)),
    )
    punctuation = DeltaPunctuation()
    app = SimpleNamespace(extensions={"providers": SimpleNamespace(punctuation=punctuation)})
    session = {
        "asr": StreamingASR(),
        "punctuation": punctuation,
        "utterance_id": 1,
        "turn_revision": 0,
        "event_sequence": 0,
        "last_stable_display": "",
    }
    pipeline = SpeechPipeline()
    pipeline._drain_asr(app, "meeting-1", session, b"", start_ms=0, end_ms=40)
    pipeline._drain_asr(app, "meeting-1", session, b"", start_ms=40, end_ms=80)

    partials = [payload["text"] for event, payload in events if event == "transcript_partial"]
    assert partials == ["嗯", "嗯就是"]


def test_funasr_sessions_isolate_stream_state_per_meeting(app):
    from app.providers.diarization import EmbeddingClusterProvider
    from app.providers.funasr import FunASRStreamingProvider
    from app.providers.punctuation import FunASRPunctuationProvider

    registry = app.extensions["providers"]
    old_backend, old_asr, old_dia, old_punctuation = (
        registry.speech_backend,
        registry.asr,
        registry.diarization,
        registry.punctuation,
    )
    try:
        registry.speech_backend = "funasr"
        registry.asr = FunASRStreamingProvider()
        registry.punctuation = FunASRPunctuationProvider()
        registry.diarization = EmbeddingClusterProvider()
        pipeline = SpeechPipeline(app)

        first = pipeline._session(app, "meeting-a")
        second = pipeline._session(app, "meeting-b")

        assert first["asr"] is not second["asr"]
        assert first["asr"]._cache is not second["asr"]._cache
        assert first["punctuation"] is not second["punctuation"]
        assert first["diarization"] is not second["diarization"]
    finally:
        registry.speech_backend = old_backend
        registry.asr = old_asr
        registry.diarization = old_dia
        registry.punctuation = old_punctuation
        app.extensions.get("speech_sessions", {}).clear()


def test_funasr_session_uses_global_and_project_terminology_only(app):
    from app.models import Meeting, Project, Terminology, User
    from app.providers.diarization import EmbeddingClusterProvider
    from app.providers.funasr import FunASRStreamingProvider
    from app.providers.punctuation import FunASRPunctuationProvider
    from app.web.extensions import db

    registry = app.extensions["providers"]
    old_backend, old_asr, old_dia, old_punctuation = (
        registry.speech_backend,
        registry.asr,
        registry.diarization,
        registry.punctuation,
    )
    try:
        registry.speech_backend = "funasr"
        registry.asr = FunASRStreamingProvider(hotwords="configured-term")
        registry.punctuation = FunASRPunctuationProvider()
        registry.diarization = EmbeddingClusterProvider()
        admin = User.query.filter_by(email="admin@local").one()
        current_project = Project(name="hotword-current")
        other_project = Project(name="hotword-other")
        db.session.add_all([current_project, other_project])
        db.session.flush()
        meeting = Meeting(
            name="hotword-session",
            created_by=admin.id,
            project_id=current_project.id,
        )
        db.session.add_all(
            [
                meeting,
                Terminology(term="global-term", aliases=["global-alias"], priority=10),
                Terminology(
                    term="project-term",
                    aliases=["project-alias", "global-alias"],
                    project_id=current_project.id,
                    priority=20,
                ),
                Terminology(term="foreign-term", project_id=other_project.id, priority=100),
                Terminology(term="disabled-term", enabled=False, priority=50),
            ]
        )
        db.session.flush()

        pipeline = SpeechPipeline(app)
        session = pipeline._session(app, meeting.id)

        assert session["asr"].hotwords.split() == [
            "configured-term",
            "project-term",
            "project-alias",
            "global-alias",
            "global-term",
        ]
        db.session.add(
            Terminology(term="next-turn-term", project_id=current_project.id, priority=30)
        )
        db.session.flush()
        pipeline._refresh_meeting_asr_hotwords(meeting.id, session)
        assert session["asr"].hotwords.split()[1] == "next-turn-term"
    finally:
        registry.speech_backend = old_backend
        registry.asr = old_asr
        registry.diarization = old_dia
        registry.punctuation = old_punctuation
        app.extensions.get("speech_sessions", {}).clear()
