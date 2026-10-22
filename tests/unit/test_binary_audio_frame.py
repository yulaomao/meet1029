"""Realtime audio_frame is binary PCM; server stores it against the joined meeting."""

from pathlib import Path

from app.models import User
from app.services.meeting import MeetingService
from app.web.socket_events import parse_audio_frame, store_pcm


def test_parse_audio_frame_binary_uses_session_meeting_id():
    pcm = b"\x00\x01\x02\x03" * 10
    mid, out = parse_audio_frame(pcm, meeting_id_from_session="meet-1")
    assert mid == "meet-1"
    assert out == pcm


def test_parse_audio_frame_legacy_dict_still_works():
    pcm = b"\x10\x20"
    mid, out = parse_audio_frame({"meeting_id": "m2", "pcm": pcm})
    assert mid == "m2"
    assert out == pcm


def test_store_pcm_appends(app):
    with app.app_context():
        store_pcm("mid", b"abc")
        store_pcm("mid", b"def")
        assert bytes(app.extensions["pcm_buffers"]["mid"]) == b"abcdef"


def test_socket_binary_audio_frame_stores_pcm(app, auth_admin):
    from app.web.extensions import socketio

    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = MeetingService().create(admin, name="pcm")
        MeetingService().set_consent(m, True)
        MeetingService().start(m, admin, client_id="c1")
        mid = m.id
    sio = socketio.test_client(app, namespace="/live", flask_test_client=auth_admin)
    sio.emit("join_meeting", {"meeting_id": mid}, namespace="/live")
    pcm = b"\x01\x00" * 80
    sio.emit("audio_frame", pcm, namespace="/live")
    with app.app_context():
        buf = app.extensions.get("pcm_buffers", {}).get(mid)
        assert buf is not None
        assert bytes(buf) == pcm
    sio.disconnect(namespace="/live")


def test_socket_reconnect_and_rejoin_continues_spooling_audio(app, auth_admin):
    """A reconnect creates a new SID, so the client must rejoin before binary frames."""
    from app.web.extensions import socketio

    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        meeting = MeetingService().create(admin, name="pcm-reconnect")
        MeetingService().set_consent(meeting, True)
        MeetingService().start(meeting, admin, client_id="reconnect-client")
        meeting_id = meeting.id

    first = socketio.test_client(app, namespace="/live", flask_test_client=auth_admin)
    first.emit("join_meeting", {"meeting_id": meeting_id}, namespace="/live")
    first_pcm = b"\x01\x00" * 80
    first.emit("audio_frame", first_pcm, namespace="/live")
    first.disconnect(namespace="/live")

    reconnected = socketio.test_client(app, namespace="/live", flask_test_client=auth_admin)
    reconnected.emit("join_meeting", {"meeting_id": meeting_id}, namespace="/live")
    second_pcm = b"\x02\x00" * 80
    reconnected.emit("audio_frame", second_pcm, namespace="/live")

    with app.app_context():
        buf = app.extensions["pcm_buffers"][meeting_id]
        assert bytes(buf) == first_pcm + second_pcm

    reconnected.disconnect(namespace="/live")


def test_live_js_emits_binary_not_json_wrapper():
    src = Path("app/web/static/js/live.js").read_text(encoding="utf-8")
    assert "audio_frame" in src
    assert 'socket.emit("audio_frame", bytes)' in src
    assert 'socket.emit("audio_frame", payload)' in src
    assert "sendPcmFrame(ev.data)" in src
    assert "{ meeting_id: meetingId, pcm:" not in src
    assert "audio_b64" in src
    assert "meeting_state_patch" in src
    assert "ai_token" in src
    assert "host_changed" in src or "host_transferred" in src
    assert "queue.restore" in src
    assert "force: false" in src or "force:false" in src
    assert "startRecorder" in src


def test_parse_audio_frame_list_of_ints():
    mid, out = parse_audio_frame([1, 2, 3, 4], meeting_id_from_session="m3")
    assert mid == "m3"
    assert out == bytes([1, 2, 3, 4])


def test_parse_audio_frame_one_element_bytes_list():
    pcm = b"\x00\x01" * 4
    mid, out = parse_audio_frame([pcm], meeting_id_from_session="m4")
    assert mid == "m4"
    assert out == pcm


def test_parse_audio_frame_dict_pcm_as_int_list():
    mid, out = parse_audio_frame({"meeting_id": "m5", "pcm": [9, 8, 7]})
    assert mid == "m5"
    assert out == bytes([9, 8, 7])
