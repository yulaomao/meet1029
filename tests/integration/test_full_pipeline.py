"""Create → start → upload N chunks → complete → assemble wav → Fake pipeline → COMPLETED."""

from __future__ import annotations

import struct
import wave
from io import BytesIO

from app.models import ActionItem, Meeting, MeetingImportJob, MeetingSummary
from app.services.audio_import import AudioImportService
from app.web.extensions import db


def _wav_bytes(ms: int = 200) -> bytes:
    n = int(16000 * ms / 1000)
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<" + "h" * n, *([0] * n)))
    return buf.getvalue()


def test_full_path_create_start_upload_complete(auth_admin, app):
    r = auth_admin.post("/api/meetings", json={"name": "集成会议"})
    assert r.status_code == 201
    mid = r.get_json()["id"]
    r = auth_admin.post(f"/api/meetings/{mid}/start", json={"consent": True, "client_id": "host1"})
    assert r.status_code == 200
    wav = _wav_bytes()
    for seq in range(3):
        resp = auth_admin.post(
            f"/api/meetings/{mid}/audio/chunks",
            data={"seq": str(seq), "start_ms": str(seq * 200), "mime": "audio/wav", "client_id": "host1",
                  "file": (BytesIO(wav), f"chunk_{seq:06d}.wav")},
            content_type="multipart/form-data",
        )
        assert resp.get_json()["ack"] is True
    # re-upload same seq is idempotent
    resp = auth_admin.post(
        f"/api/meetings/{mid}/audio/chunks",
        data={"seq": "1", "start_ms": "200", "mime": "audio/wav", "client_id": "host1",
              "file": (BytesIO(wav), "chunk_000001.wav")},
        content_type="multipart/form-data",
    )
    assert resp.get_json()["ack"] is True
    assert resp.get_json()["idempotent"] is True

    st = auth_admin.get(f"/api/meetings/{mid}/audio/chunks/status")
    assert st.get_json()["count"] == 3

    done = auth_admin.post(f"/api/meetings/{mid}/audio/complete", json={"client_id": "host1"})
    assert done.get_json()["complete"] is True
    assert done.get_json()["master"]

    stop = auth_admin.post(f"/api/meetings/{mid}/stop", json={"force": True})
    assert stop.status_code == 200
    with app.app_context():
        m = db.session.get(Meeting, mid)
        assert m.status == "COMPLETED"
        assert MeetingSummary.query.filter_by(meeting_id=mid).count() >= 1
        assert ActionItem.query.filter_by(meeting_id=mid).count() >= 1


def test_stop_queues_live_post_processing_without_waiting_for_final_asr(auth_admin, app):
    created = auth_admin.post("/api/meetings", json={"name": "后台处理会议"}).get_json()
    mid = created["id"]
    assert auth_admin.post(f"/api/meetings/{mid}/start", json={"consent": True, "client_id": "host1"}).status_code == 200
    wav = _wav_bytes(300)
    assert auth_admin.post(
        f"/api/meetings/{mid}/audio/chunks",
        data={"seq": "0", "start_ms": "0", "mime": "audio/wav", "client_id": "host1", "file": (BytesIO(wav), "chunk.wav")},
        content_type="multipart/form-data",
    ).status_code == 200
    response = auth_admin.post(f"/api/meetings/{mid}/stop", json={"force": False})
    assert response.status_code == 202
    assert response.get_json()["stage"] == "queued"
    with app.app_context():
        job = MeetingImportJob.query.filter_by(meeting_id=mid).first()
        assert job and job.kind == "live" and job.state == "QUEUED"
        assert db.session.get(Meeting, mid).status == "PROCESSING"
        # The same single worker handles both imported audio and a finished
        # browser recording, so completion is durable rather than browser-bound.
        assert AudioImportService(app).run_next() is True
        assert db.session.get(Meeting, mid).status == "COMPLETED"


def test_host_transfer_api(auth_admin):
    r = auth_admin.post("/api/meetings", json={"name": "切换主持"})
    mid = r.get_json()["id"]
    auth_admin.post(f"/api/meetings/{mid}/start", json={"consent": True, "client_id": "mac"})
    tr = auth_admin.post(f"/api/meetings/{mid}/host-transfer", json={"client_id": "phone"})
    assert tr.status_code == 200
    assert tr.get_json()["host_client_id"] == "phone"
    assert tr.get_json()["gap_ms"] <= 2000


def test_health_endpoint(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert "status" in body
    assert "database" in body
