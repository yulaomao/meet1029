from __future__ import annotations

import struct
import wave
from io import BytesIO
from pathlib import Path

import pytest

from app.media import resolve_ffprobe
from app.models import Meeting, MeetingImportJob, TranscriptSegment, User
from app.services.audio_import import AudioImportService
from app.services.meeting import MeetingService
from app.services.offline_speaker import OfflineSpeakerDiarizer
from app.services.transcript import TranscriptService
from app.web.extensions import db


def _wav_bytes(ms: int = 1000) -> bytes:
    samples = int(16000 * ms / 1000)
    out = BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(struct.pack("<" + "h" * samples, *([0] * samples)))
    return out.getvalue()


def test_import_error_for_unavailable_local_ai_service_is_actionable():
    import httpx

    error = AudioImportService._safe_error(httpx.ConnectError("connection refused"))
    assert "本机 AI 服务暂不可用" in error
    assert "逐字稿已保留" in error
    assert "重新处理" in error


@pytest.mark.skipif(not resolve_ffprobe(), reason="ffprobe is required for import validation")
def test_import_upload_runs_local_pipeline_and_exports(auth_admin, app):
    response = auth_admin.post(
        "/api/meetings/import",
        data={
            "audio": (BytesIO(_wav_bytes(900)), "demo.wav"),
            "name": "",
            "expected_speakers": "2",
            "meeting_type": "技术讨论",
            "consent": "on",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    payload = response.get_json()
    meeting_id = payload["meeting_id"]
    with app.app_context():
        service = AudioImportService(app)
        assert service.run_next() is True
        meeting = db.session.get(Meeting, meeting_id)
        job = MeetingImportJob.query.filter_by(meeting_id=meeting_id).first()
        assert meeting.status == "COMPLETED"
        assert job.state == "COMPLETED"
        assert app.extensions["audio_store"].master_path(meeting_id).exists()
        export = app.extensions["audio_store"].read_json(meeting_id, "final_transcript.json")
        assert export and export["segments"]
        assert TranscriptSegment.query.filter_by(meeting_id=meeting_id, source="final").count() > 0


def test_import_rejects_missing_consent_or_unsupported_file(auth_admin):
    no_consent = auth_admin.post(
        "/api/meetings/import",
        data={"audio": (BytesIO(_wav_bytes()), "demo.wav")},
        content_type="multipart/form-data",
    )
    assert no_consent.status_code == 400
    wrong = auth_admin.post(
        "/api/meetings/import",
        data={"audio": (BytesIO(b"not audio"), "demo.txt"), "consent": "on"},
        content_type="multipart/form-data",
    )
    assert wrong.status_code == 400


@pytest.mark.skipif(not resolve_ffprobe(), reason="ffprobe is required for import validation")
def test_import_preserves_supported_extension_for_unicode_filename(auth_admin, app):
    response = auth_admin.post(
        "/api/meetings/import",
        data={
            "audio": (BytesIO(_wav_bytes()), "机械臂失控问题应对方案探讨.m4a"),
            "consent": "on",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    meeting_id = response.get_json()["meeting_id"]
    with app.app_context():
        job = MeetingImportJob.query.filter_by(meeting_id=meeting_id).one()
        assert job.original_filename == "机械臂失控问题应对方案探讨.m4a"
        assert Path(job.source_path).suffix == ".m4a"


def test_retry_keeps_original_source_and_requeues_failed_import(auth_admin, app):
    with app.app_context():
        user = User.query.filter_by(email="admin@local").first()
        meeting = MeetingService().create(user, name="retry")
        source = app.extensions["audio_store"].source_dir(meeting.id) / "import.wav"
        source.write_bytes(_wav_bytes())
        job = MeetingImportJob(
            meeting_id=meeting.id,
            original_filename="import.wav",
            source_path=str(source),
            state="FAILED",
            stage="failed",
            error="test",
        )
        meeting.status = "FAILED"
        db.session.add(job)
        db.session.commit()
        meeting_id = meeting.id
    response = auth_admin.post(f"/api/meetings/{meeting_id}/import/retry")
    assert response.status_code == 202
    with app.app_context():
        job = MeetingImportJob.query.filter_by(meeting_id=meeting_id).first()
        assert job.state == "QUEUED"
        assert job.error == ""
        assert app.extensions["audio_store"].source_dir(meeting_id).joinpath("import.wav").exists()


@pytest.mark.skipif(not resolve_ffprobe(), reason="ffprobe is required for import normalization")
def test_import_passes_normalized_master_to_post_meeting_graph(app, monkeypatch):
    """Imported audio must never fall back to browser-capture assembly/silence."""
    captured = {}

    class FakeGraph:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _meeting_id, master_path=None):
            captured["master_path"] = master_path

    with app.app_context():
        user = User.query.filter_by(email="admin@local").first()
        meeting = MeetingService().create(user, name="normalized master")
        source = app.extensions["audio_store"].source_dir(meeting.id) / "import.wav"
        source.write_bytes(_wav_bytes())
        db.session.add(
            MeetingImportJob(
                meeting_id=meeting.id,
                original_filename="import.wav",
                source_path=str(source),
                state="QUEUED",
                stage="queued",
            )
        )
        db.session.commit()
        monkeypatch.setattr("app.services.audio_import.PostMeetingGraph", FakeGraph)
        assert AudioImportService(app).run_next() is True
        assert captured["master_path"] == str(app.extensions["audio_store"].master_path(meeting.id))


def test_offline_speaker_uses_anonymous_clusters_for_unregistered_audio(app, monkeypatch):
    with app.app_context():
        user = User.query.filter_by(email="admin@local").first()
        meeting = MeetingService().create(user, name="speaker", expected_speakers=2)
        master = app.extensions["audio_store"].master_path(meeting.id)
        master.write_bytes(_wav_bytes(6000))
        first = TranscriptService().add_segment(meeting.id, "第一位发言", start_ms=0, end_ms=2000, source="final")
        second = TranscriptService().add_segment(meeting.id, "第二位发言", start_ms=2200, end_ms=4500, source="final")
        diarizer = OfflineSpeakerDiarizer(app)
        values = iter([[1.0, 0.0], [0.0, 1.0]])
        monkeypatch.setattr(diarizer, "_vector", lambda _master, _segment: next(values))
        diarizer.apply(meeting.id, master)
        rows = {row.id: row for row in TranscriptSegment.query.filter_by(meeting_id=meeting.id).all()}
        assert rows[first.id].speaker_label == "说话人 1"
        assert rows[second.id].speaker_label == "说话人 2"
