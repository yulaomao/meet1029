"""Playwright e2e. Seeded fixtures so a real microphone is not required."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")
pytestmark = pytest.mark.e2e

from app.models import ActionItem, Decision, Meeting, MeetingImportJob, MeetingSummary, TranscriptSegment, User
from app.models.enums import MeetingStatus
from app.web.extensions import db
from tests.e2e.browser import launch_chromium


@pytest.fixture()
def completed_meeting(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        m = Meeting(
            name="膝关节项目技术讨论",
            created_by=admin.id,
            status=MeetingStatus.COMPLETED.value,
            consent_given=True,
        )
        db.session.add(m)
        db.session.flush()
        db.session.add(
            TranscriptSegment(
                meeting_id=m.id,
                raw_text="这个针尖球心补偿目前看还是需要重新标定。",
                display_text="这个针尖球心补偿目前看还是需要重新标定。",
                speaker_label="张工",
                start_ms=24110,
                source="final",
            )
        )
        db.session.add(MeetingSummary(meeting_id=m.id, summary="决定重新采集5组数据。"))
        db.session.add(Decision(meeting_id=m.id, text="重新采集5组数据"))
        db.session.add(ActionItem(meeting_id=m.id, description="完成针尖球心补偿验证", owner="张工"))
        db.session.commit()
        return m.id


def test_meeting_detail_transcript_scrolls_without_moving_summary(live_server, app, completed_meeting):
    from playwright.sync_api import sync_playwright

    with app.app_context():
        for index in range(60):
            db.session.add(
                TranscriptSegment(
                    meeting_id=completed_meeting,
                    raw_text=f"第 {index + 1} 段会议逐字稿内容。" * 8,
                    display_text=f"第 {index + 1} 段会议逐字稿内容。" * 8,
                    speaker_label="张工",
                    start_ms=index * 10000,
                    end_ms=(index + 1) * 10000,
                    source="final",
                )
            )
        db.session.commit()

    base = live_server.url()
    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _login(page, base)
        page.goto(f"{base}/meetings/{completed_meeting}")
        metrics = page.evaluate(
            """() => {
              const transcript = document.querySelector('#transcript-list');
              const notes = document.querySelector('.notes-pane');
              const notesTopBefore = notes.getBoundingClientRect().top;
              transcript.scrollTop = transcript.scrollHeight;
              return {
                transcriptClientHeight: transcript.clientHeight,
                transcriptScrollHeight: transcript.scrollHeight,
                transcriptScrollTop: transcript.scrollTop,
                notesTopBefore,
                notesTopAfter: notes.getBoundingClientRect().top,
                pageScrollTop: document.scrollingElement.scrollTop
              };
            }"""
        )
        assert metrics["transcriptScrollHeight"] > metrics["transcriptClientHeight"]
        assert metrics["transcriptScrollTop"] > 0
        assert metrics["notesTopAfter"] == metrics["notesTopBefore"]
        assert metrics["pageScrollTop"] == 0
        browser.close()


def _login(page, base):
    page.goto(base + "/login")
    page.fill("input[name=email]", "admin@local")
    page.fill("input[name=password]", "admin123")
    page.click("button[type=submit]")
    page.wait_for_url("**/")


def test_import_failure_refreshes_transcript_saved_before_ai_summary_error(live_server, app):
    """A failed summary stage must refresh the details page to show saved ASR."""
    from playwright.sync_api import sync_playwright

    with app.app_context():
        admin = User.query.filter_by(email="admin@local").first()
        meeting = Meeting(
            name="导入失败仍显示逐字稿",
            created_by=admin.id,
            status=MeetingStatus.PROCESSING.value,
            consent_given=True,
        )
        db.session.add(meeting)
        db.session.flush()
        db.session.add(
            MeetingImportJob(
                meeting_id=meeting.id,
                original_filename="recording.m4a",
                source_path="/test/recording.m4a",
                state="RUNNING",
                stage="summarizing",
                progress=78,
            )
        )
        db.session.commit()
        meeting_id = meeting.id

    base = live_server.url()
    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page()
        _login(page, base)
        page.goto(f"{base}/meetings/{meeting_id}")
        assert page.get_by_text("0 段", exact=True).is_visible()

        # Simulate ASR being persisted before the later AI summary connection fails.
        with app.app_context():
            meeting = db.session.get(Meeting, meeting_id)
            job = MeetingImportJob.query.filter_by(meeting_id=meeting_id).one()
            meeting.status = MeetingStatus.FAILED.value
            job.state = "FAILED"
            job.stage = "failed"
            job.error = "本机 AI 服务暂不可用，逐字稿已保留。"
            db.session.add(
                TranscriptSegment(
                    meeting_id=meeting_id,
                    raw_text="这段逐字稿已保存。",
                    display_text="这段逐字稿已保存。",
                    speaker_label="说话人 1",
                    start_ms=0,
                    end_ms=1200,
                    source="final",
                )
            )
            db.session.commit()

        page.get_by_text("1 段", exact=True).wait_for(timeout=10000)
        assert page.get_by_text("这段逐字稿已保存。", exact=True).is_visible()
        assert page.get_by_text("本机 AI 服务暂不可用，逐字稿已保留。", exact=False).is_visible()
        browser.close()


def test_login_create_live_status_bar(live_server):
    from playwright.sync_api import sync_playwright

    base = live_server.url()
    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page()
        _login(page, base)
        page.goto(base + "/meetings/new")
        page.fill("input[name=name]", "E2E 会议")
        page.click("#review-start")
        page.wait_for_selector("#start-confirm[open]")
        page.click("#confirm-start")
        page.wait_for_selector("#status-bar")
        assert page.locator("#status-bar").is_visible()
        assert page.locator("#ai-state").count() == 1
        browser.close()


def test_virtual_microphone_audio_reaches_live_socket_spool(live_server):
    """A generated virtual mic signal must traverse getUserMedia and AudioWorklet."""
    import time

    from playwright.sync_api import sync_playwright

    from app.models import Meeting
    from app.web.extensions import db

    base = live_server.url()
    with sync_playwright() as p:
        browser = launch_chromium(p, args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context()
        context.add_init_script(
            """(() => {
              Object.defineProperty(navigator, 'wakeLock', {
                configurable: true,
                value: {request: async () => { throw new Error('wake lock unavailable in headless browser'); }}
              });
              const devices = navigator.mediaDevices || {};
              const contexts = [];
              Object.defineProperty(navigator, 'mediaDevices', {value: devices, configurable: true});
              devices.enumerateDevices = async () => [{kind: 'audioinput', deviceId: 'virtual-mic', label: 'Virtual test microphone'}];
              devices.getUserMedia = async () => {
                const audio = new AudioContext();
                const output = audio.createMediaStreamDestination();
                const oscillator = audio.createOscillator();
                oscillator.frequency.value = 440;
                oscillator.connect(output);
                oscillator.start();
                contexts.push(audio);
                window.__virtualMicContexts = contexts;
                return output.stream;
              };
            })();"""
        )
        page = context.new_page()
        _login(page, base)
        page.goto(base + "/meetings/new")
        page.fill("input[name=name]", "虚拟麦克风采集回归")
        page.click("#review-start")
        page.click("#confirm-start")
        page.wait_for_selector("#live-root")
        meeting_id = page.locator("#live-root").get_attribute("data-meeting-id")
        assert meeting_id

        deadline = time.monotonic() + 10
        pcm_path = None
        while time.monotonic() < deadline:
            with live_server.app.app_context():
                meeting = db.session.get(Meeting, meeting_id)
                if meeting is not None:
                    pcm_path = live_server.app.extensions["audio_store"].meeting_dir(meeting_id) / "realtime.pcm"
                    if pcm_path.exists() and pcm_path.stat().st_size >= 3200:
                        break
            page.wait_for_timeout(100)

        assert pcm_path is not None and pcm_path.exists(), "virtual microphone produced no PCM spool"
        assert pcm_path.stat().st_size >= 3200, "less than one 100 ms PCM frame reached the server"
        page.wait_for_function("document.querySelector('#conn-state')?.textContent === '在线'")
        page.wait_for_timeout(150)
        assert page.locator("#conn-state").inner_text() == "在线"
        context.close()
        browser.close()


def test_live_partial_revises_monotonically_and_final_replaces_it(live_server):
    """Recorded fixture events exercise the live caption UI without user speech."""
    import time

    from app.web.socket_events import SID_MEETING
    from app.web.extensions import socketio
    from playwright.sync_api import sync_playwright

    base = live_server.url()
    with sync_playwright() as p:
        browser = launch_chromium(p)
        context = browser.new_context()
        context.add_init_script(
            "Object.defineProperty(navigator, 'mediaDevices', {value: undefined, configurable: true});"
        )
        page = context.new_page()
        _login(page, base)
        page.goto(base + "/meetings/new")
        page.fill("input[name=name]", "字幕事件回归")
        page.click("#review-start")
        page.click("#confirm-start")
        page.wait_for_selector("#live-root")
        meeting_id = page.locator("#live-root").get_attribute("data-meeting-id")
        assert meeting_id
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and meeting_id not in SID_MEETING.values():
            page.wait_for_timeout(50)
        assert meeting_id in SID_MEETING.values(), "browser never joined the live Socket.IO room"

        socketio.emit(
            "transcript_partial",
            {"meeting_id": meeting_id, "utterance_id": "fixture-u1", "turn_revision": 1, "text": "这是一条实时字幕"},
            namespace="/live",
            to=meeting_id,
        )
        partial = page.locator("article.seg.partial-live p")
        partial.wait_for(state="visible")
        assert partial.inner_text() == "这是一条实时字幕 ▌"

        socketio.emit(
            "transcript_partial",
            {"meeting_id": meeting_id, "utterance_id": "fixture-u1", "turn_revision": 2, "text": "这是一条实时字幕正在更新"},
            namespace="/live",
            to=meeting_id,
        )
        page.wait_for_function(
            "document.querySelector('article.seg.partial-live p')?.textContent === '这是一条实时字幕正在更新 ▌'"
        )

        socketio.emit(
            "transcript_final",
            {
                "meeting_id": meeting_id,
                "id": "fixture-final-1",
                "utterance_id": "fixture-u1",
                "turn_revision": 3,
                "start_ms": 4000,
                "speaker": "测试说话人",
                "text": "这是一条实时字幕，已经收束。",
            },
            namespace="/live",
            to=meeting_id,
        )
        final = page.locator("#seg-fixture-final-1")
        final.wait_for(state="visible")
        assert final.locator("p").inner_text() == "这是一条实时字幕，已经收束。"
        assert page.locator("article.seg.partial-live").count() == 0

        socketio.emit(
            "asr_metrics",
            {"meeting_id": meeting_id, "queue_lag_ms": 2360, "processing_ms": 950},
            namespace="/live",
            to=meeting_id,
        )
        page.wait_for_function("document.querySelector('#asr-lag')?.textContent === '2.4s'")
        assert "2360ms" in page.locator("#asr-lag").get_attribute("title")
        assert "950ms" in page.locator("#asr-lag").get_attribute("title")

        socketio.emit(
            "transcript_partial",
            {"meeting_id": meeting_id, "utterance_id": "fixture-u1", "turn_revision": 2, "text": "过期字幕"},
            namespace="/live",
            to=meeting_id,
        )
        page.wait_for_timeout(100)
        assert final.locator("p").inner_text() == "这是一条实时字幕，已经收束。"
        context.close()
        browser.close()


def test_post_meeting_dual_pane(live_server, completed_meeting):
    from playwright.sync_api import sync_playwright

    base = live_server.url()
    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page()
        _login(page, base)
        page.goto(base + f"/meetings/{completed_meeting}")
        page.wait_for_selector(".dual-pane")
        assert "AI 会议结论" in page.content()
        assert "逐字稿" in page.content()
        assert page.locator(".citation").count() >= 1
        browser.close()


def test_mobile_viewport_health_bar(live_server):
    from playwright.sync_api import sync_playwright

    base = live_server.url()
    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _login(page, base)
        page.goto(base + "/meetings/new")
        page.fill("input[name=name]", "手机会议")
        page.click("#review-start")
        page.click("#confirm-start")
        page.wait_for_selector("#mobile-health")
        assert page.locator("#mobile-health").is_visible()
        browser.close()


def test_live_socket_rejoins_meeting_after_browser_network_recovery(live_server):
    """Browser reconnection must repeat join_meeting for its new Socket.IO SID."""
    import time

    from playwright.sync_api import sync_playwright
    from app.web.socket_events import SID_MEETING

    base = live_server.url()
    with sync_playwright() as p:
        browser = launch_chromium(p)
        context = browser.new_context()
        # The test injects deterministic PCM frames, so it does not need a
        # physical microphone or browser permission prompt.
        context.add_init_script(
            "Object.defineProperty(navigator, 'mediaDevices', {value: undefined, configurable: true});"
        )
        page = context.new_page()
        browser_errors = []
        socket_requests = []
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.on("console", lambda msg: browser_errors.append(msg.text) if msg.type == "error" else None)
        page.on("request", lambda req: socket_requests.append(req.url) if "/socket.io/" in req.url else None)
        _login(page, base)
        page.goto(base + "/meetings/new")
        page.fill("input[name=name]", "Socket.IO 重连会议")
        page.click("#review-start")
        page.click("#confirm-start")
        page.wait_for_selector("#live-root")
        meeting_id = page.locator("#live-root").get_attribute("data-meeting-id")
        assert meeting_id
        archive_uploads = []

        def acknowledge_archive_chunk(route):
            archive_uploads.append(route.request.post_data or "")
            route.fulfill(status=200, content_type="application/json", body='{"ack": true}')

        page.route("**/api/meetings/*/audio/chunks", acknowledge_archive_chunk)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and meeting_id not in SID_MEETING.values():
            page.wait_for_timeout(50)
        assert meeting_id in SID_MEETING.values(), (
            "browser never joined the live Socket.IO room; "
            f"socket requests={socket_requests!r}, browser errors={browser_errors!r}"
        )
        old_sids = {sid for sid, joined_meeting in SID_MEETING.items() if joined_meeting == meeting_id}

        context.set_offline(True)
        # A dropped long-poll is detected only after the heartbeat interval
        # and timeout. Derive the wait from Engine.IO instead of assuming one
        # timeout period is sufficient; add slack for the threaded test server.
        engineio = live_server.app.extensions["socketio"].server.eio
        disconnect_wait_s = float(engineio.ping_interval) + float(engineio.ping_timeout) + 5
        deadline = time.monotonic() + max(35, disconnect_wait_s)
        while time.monotonic() < deadline and old_sids.intersection(SID_MEETING):
            page.wait_for_timeout(50)
        assert not old_sids.intersection(SID_MEETING), "server did not clear the disconnected Socket.IO SID"
        page.wait_for_function(
            "['重连中', '网络断开'].some(text => "
            "document.querySelector('#conn-state')?.textContent.includes(text))",
            timeout=5000,
        )
        pending = page.evaluate(
            "() => { "
            "const frame = new Uint8Array(1280); "
            "for (let i = 0; i < 752; i++) { frame.fill(i & 255); window.MeetingLive.sendPcmFrame(frame); } "
            "return {bytes: window.MeetingLive.pendingRealtimePcmBytes, dropped: window.MeetingLive.droppedRealtimePcmBytes}; "
            "}"
        )
        assert pending == {"bytes": 30 * 16000 * 2, "dropped": 2 * 1280}
        # A recording chunk has already been durably saved to IndexedDB. It
        # must be uploaded automatically when the browser comes back online.
        page.evaluate(
            "async () => window.MeetingLive.queue.save({"
            "seq: 9001, start_ms: 45005000, duration_ms: 5000,"
            "mime: 'audio/webm;codecs=opus',"
            "blob: new Blob(['offline archive chunk'], {type: 'audio/webm'}),"
            "clientId: window.MeetingLive.clientId"
            "})"
        )
        assert page.evaluate("window.MeetingLive.queue.size()") == 1
        context.set_offline(False)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            sid not in old_sids and joined_meeting == meeting_id for sid, joined_meeting in SID_MEETING.items()
        ):
            page.wait_for_timeout(50)
        assert any(sid not in old_sids and joined_meeting == meeting_id for sid, joined_meeting in SID_MEETING.items())
        deadline = time.monotonic() + 5
        expected_audio = b"".join(bytes([index & 255]) * 1280 for index in range(2, 752))
        while time.monotonic() < deadline:
            buffered = live_server.app.extensions.get("pcm_buffers", {}).get(meeting_id)
            if buffered is not None and bytes(buffered) == expected_audio:
                break
            page.wait_for_timeout(50)
        assert bytes(live_server.app.extensions["pcm_buffers"][meeting_id]) == expected_audio
        assert page.evaluate("window.MeetingLive.pendingRealtimePcmBytes") == 0
        assert page.evaluate("window.MeetingLive.droppedRealtimePcmBytes") == 2 * 1280
        page.wait_for_function("window.MeetingLive.queue.size() === 0", timeout=10000)
        assert len(archive_uploads) == 1
        assert "9001" in archive_uploads[0]
        pending_archive = page.evaluate(
            "async (meetingId) => {"
            "const {loadPendingFromIdb} = await import('/static/js/recorder.js');"
            "return loadPendingFromIdb(meetingId, window.MeetingLive.clientId);"
            "}",
            meeting_id,
        )
        assert pending_archive == []
        context.close()
        browser.close()
