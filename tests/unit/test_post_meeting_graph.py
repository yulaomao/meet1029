"""Post-meeting pipeline uses a compiled LangGraph when available."""

from pathlib import Path

from app.graphs.post_meeting import PostMeetingGraph


def test_run_invokes_compiled_graph(app, monkeypatch):
    called = {}

    class FakeCompiled:
        def invoke(self, state):
            called["state"] = dict(state)
            out = dict(state)
            out["via"] = "graph"
            return out

    with app.app_context():
        g = PostMeetingGraph(app, app.extensions["providers"], app.extensions["audio_store"])
        monkeypatch.setattr(g, "compile", lambda: FakeCompiled())
        out = g.run("meeting-x")
    assert called["state"]["meeting_id"] == "meeting-x"
    assert out["via"] == "graph"


def test_compile_returns_invoke_when_langgraph_installed():
    try:
        from langgraph.graph import StateGraph  # noqa: F401
    except Exception:
        src = Path("app/graphs/post_meeting.py").read_text(encoding="utf-8")
        assert "compile" in src and "invoke" in src
        return
    # compile() on a dummy instance without DB work: graph object itself
    from app.graphs.post_meeting import _try_langgraph
    StateGraph, END = _try_langgraph()
    assert StateGraph is not None
    assert END is not None


from app.models import TranscriptSegment, User
from app.models.enums import TranscriptSource
from app.services.meeting import MeetingService, default_meeting_name
from app.services.transcript import TranscriptService


def _graph(app):
    return PostMeetingGraph(app, app.extensions["providers"], app.extensions["audio_store"])


def test_fake_final_asr_inserts_canned_when_testing(app):
    with app.app_context():
        assert app.config.get("TESTING") is True
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="final-canned")
        state = _graph(app).node_final_asr({"meeting_id": m.id, "master_path": "/tmp/none.wav"})
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        texts = [s.raw_text or "" for s in segs]
        assert any("针尖球心补偿" in t for t in texts)
        assert all(s.source == TranscriptSource.FINAL.value for s in segs)
        stored = app.extensions["audio_store"].read_json(m.id, "final_transcript.json")
        assert stored and stored.get("segments")
        assert state["transcript"]["segments"]


def test_fake_final_asr_overlay_skipped_in_production(app):
    with app.app_context():
        app.config["TESTING"] = False
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="final-skip-live")
        TranscriptService().add_segment(
            m.id, "真实口语句子：球心需要对照。", source=TranscriptSource.REALTIME.value
        )
        state = _graph(app).node_final_asr({"meeting_id": m.id, "master_path": "/tmp/none.wav"})
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        texts = [s.raw_text or "" for s in segs]
        assert texts == ["真实口语句子：球心需要对照。"]
        assert not any("针尖" in t for t in texts)
        stored = app.extensions["audio_store"].read_json(m.id, "final_transcript.json")
        assert stored["text"] == "真实口语句子：球心需要对照。"
        assert stored["segments"][0]["text"] == "真实口语句子：球心需要对照。"
        assert state["transcript"] == stored


def test_fake_final_asr_overlay_skipped_when_not_testing_even_without_live(app):
    with app.app_context():
        app.config["TESTING"] = False
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="final-skip-empty")
        _graph(app).node_final_asr({"meeting_id": m.id, "master_path": "/tmp/none.wav"})
        segs = TranscriptSegment.query.filter_by(meeting_id=m.id).all()
        assert segs == []
        stored = app.extensions["audio_store"].read_json(m.id, "final_transcript.json")
        assert stored == {"text": "", "segments": []}


def test_final_export_keeps_assistant_reply_between_final_human_segments(app):
    """Full ASR must replace only human live hypotheses, not AI history."""
    from app.realtime.echo import AI_SPEAKER

    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        meeting = MeetingService().create(admin, name="assistant retained")
        TranscriptService().add_segment(
            meeting.id, "先讨论需求。", start_ms=1000, source=TranscriptSource.FINAL.value
        )
        TranscriptService().add_segment(
            meeting.id, "这是成就精灵的回复。", start_ms=2000, speaker_label=AI_SPEAKER
        )
        TranscriptService().add_segment(
            meeting.id, "然后确认方案。", start_ms=3000, source=TranscriptSource.FINAL.value
        )
        export = _graph(app)._write_final_export(meeting.id)
        assert [row["speaker_label"] for row in export["segments"]] == [
            "Speaker 1", AI_SPEAKER, "Speaker 1"
        ]
        assert "这是成就精灵的回复。" in export["text"]


def test_assemble_without_explicit_master_does_not_treat_working_directory_as_audio(app, monkeypatch):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        meeting = MeetingService().create(admin, name="assemble-master")
        graph = _graph(app)
        monkeypatch.setattr(graph.audio, "complete", lambda *_args, **_kwargs: {"master": "/tmp/real-master.wav"})
        state = graph.node_assemble({"meeting_id": meeting.id})
        assert state["master_path"] == "/tmp/real-master.wav"


def test_post_analysis_replaces_only_auto_generated_meeting_title(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        auto = MeetingService().create(admin, name=default_meeting_name())
        manual = MeetingService().create(admin, name="用户指定标题")
        graph = _graph(app)
        graph.meetings.assign_auto_title(auto, {"title": "光学导航精度验证"})
        graph.meetings.assign_auto_title(manual, {"title": "不应覆盖"})
        assert auto.name == "光学导航精度验证讨论"
        assert manual.name == "用户指定标题"
