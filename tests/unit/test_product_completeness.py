"""Regression tests for user-facing gaps closed after the design audit."""

from app.graphs.scheduled_task import run_tool
from app.models import Meeting, MeetingImportJob, Terminology
from app.services.summary import SummaryService
from app.services.transcript import TranscriptService


def test_terminology_screen_persists_enabled_hotword(app, auth_admin):
    response = auth_admin.post(
        "/terminology",
        data={"term": "针尖球心补偿", "aliases": "球心补偿,球心标定", "priority": "8", "enabled": "on"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        row = Terminology.query.filter_by(term="针尖球心补偿").first()
        assert row and row.enabled and row.aliases == ["球心补偿", "球心标定"]


def test_meeting_export_and_content_search(app, auth_admin):
    with app.app_context():
        meeting = Meeting(name="技术例会", created_by="admin")
        # A valid FK is unnecessary in the SQLite test double, but use a seeded
        # account ID from the session created by the login fixture when available.
        from app.models import User

        meeting.created_by = User.query.filter_by(email="admin@local").first().id
        from app.web.extensions import db

        db.session.add(meeting)
        db.session.commit()
        TranscriptService().add_segment(meeting.id, "讨论针尖球心补偿验证", speaker_label="张工", start_ms=1200)
        meeting_id = meeting.id
    result = auth_admin.get("/meetings?q=球心补偿")
    assert result.status_code == 200
    assert "技术例会" in result.get_data(as_text=True)
    exported = auth_admin.get(f"/meetings/{meeting_id}/export?format=json")
    assert exported.status_code == 200
    assert exported.is_json
    assert exported.json["segments"][0]["raw_text"] == "讨论针尖球心补偿验证"


def test_completed_import_detail_does_not_render_the_polling_panel(app, auth_admin):
    """A completed job must not schedule a new detail-page reload forever."""
    with app.app_context():
        from app.models import User
        from app.web.extensions import db

        meeting = Meeting(name="已完成导入", created_by=User.query.filter_by(email="admin@local").first().id)
        db.session.add(meeting)
        db.session.flush()
        db.session.add(
            MeetingImportJob(
                meeting_id=meeting.id,
                original_filename="finished.wav",
                source_path="/tmp/finished.wav",
                state="COMPLETED",
                stage="completed",
                progress=100,
            )
        )
        db.session.commit()
        meeting_id = meeting.id

    page = auth_admin.get(f"/meetings/{meeting_id}").get_data(as_text=True)
    assert 'id="import-job"' not in page
    assert "window.location.reload()" not in page


def test_summary_recovers_a_valid_citation_when_model_omits_one(app):
    with app.app_context():
        from app.models import User
        from app.web.extensions import db

        meeting = Meeting(name="可追溯会议", created_by=User.query.filter_by(email="admin@local").first().id)
        db.session.add(meeting)
        db.session.commit()
        segment = TranscriptService().add_segment(meeting.id, "重新采集五组数据并与旧算法对照", speaker_label="张工")
        summary = SummaryService(app.extensions["providers"].llm).analyze(meeting)
        assert summary.id
        from app.models import Decision

        decision = Decision.query.filter_by(meeting_id=meeting.id).first()
        assert decision.source_segment_id == segment.id


def test_summary_uses_a_grounded_excerpt_when_model_omits_summary(app):
    with app.app_context():
        from app.models import User
        from app.providers.llm import FakeLLMProvider
        from app.services.meeting import MeetingService

        meeting = MeetingService().create(User.query.filter_by(email="admin@local").first(), name="导入摘要")
        TranscriptService().add_segment(meeting.id, "请检查导入录音为什么没有生成会议内容。", speaker_label="说话人 1")
        payload = {"speaker_views": [], "topics": [], "decisions": [], "action_items": []}
        summary = SummaryService(FakeLLMProvider(payload)).analyze(meeting)
        assert "会议逐字稿已生成" in summary.summary
        assert "导入录音" in summary.summary


def test_scheduled_task_permission_list_is_a_real_allow_list(app):
    with app.app_context():
        denied = run_tool("send_notification", {"title": "不应发送"}, allowed=["list_meetings"])
        assert denied["error"] == "permission denied"


def test_mediarecorder_chunk_fallback_decodes_local_pcm(app, monkeypatch):
    from types import SimpleNamespace

    from app.services.speech_pipeline import SpeechPipeline

    with app.app_context():
        providers = app.extensions["providers"]
        original_backend, original_asr = providers.speech_backend, providers.asr
        providers.speech_backend, providers.asr = "funasr", object()
        meeting_dir = app.extensions["audio_store"].meeting_dir("fallback-meeting")
        source = meeting_dir / "source" / "chunk_000001.webm"
        source.write_bytes(b"webm-placeholder")
        monkeypatch.setattr("app.services.speech_pipeline.resolve_ffmpeg", lambda: "/usr/local/bin/ffmpeg")
        monkeypatch.setattr(
            "app.services.speech_pipeline.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"\x01\x00" * 160),
        )
        try:
            pcm = SpeechPipeline(app)._pcm_for_chunk(
                app,
                SimpleNamespace(meeting_id="fallback-meeting", seq=1, path=str(source)),
            )
        finally:
            providers.speech_backend, providers.asr = original_backend, original_asr
        assert pcm == b"\x01\x00" * 160
