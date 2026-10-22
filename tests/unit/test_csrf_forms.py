"""HTML forms must send a CSRF token when WTF_CSRF_ENABLED is on."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import TestConfig
from app.providers.llm import FakeLLMProvider
from app.web.extensions import db
from app.web.factory import create_app, seed_users


def _csrf(html: bytes | str) -> str:
    text = html.decode("utf-8") if isinstance(html, (bytes, bytearray)) else html
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', text)
    if not m:
        m = re.search(r'value="([^"]+)"[^>]*name="csrf_token"', text)
    assert m, text[:800]
    return m.group(1)


def test_meeting_forms_include_csrf_partial():
    root = Path("app/web/templates")
    for rel in (
        "meetings/new.html",
        "meetings/prepare.html",
        "login.html",
        "knowledge/list.html",
        "tasks/list.html",
        "enroll.html",
        "base.html",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        if rel == "base.html":
            assert 'name="csrf-token"' in text
            assert "js/csrf.js" in text
        else:
            assert "partials/csrf.html" in text


def test_start_meeting_csrf_missing_is_400_then_ok_with_token(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    class Local(TestConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite://"
        DATA_DIR = data_dir
        WTF_CSRF_ENABLED = True
        TESTING = True
        LLM_BACKEND = "fake"
        SPEECH_BACKEND = "fake"

    application = create_app(Local, DATA_DIR=str(data_dir), SQLALCHEMY_DATABASE_URI="sqlite://")
    application.config["WTF_CSRF_ENABLED"] = True
    application.extensions["providers"].llm = FakeLLMProvider()
    with application.app_context():
        db.create_all()
        seed_users()

    client = application.test_client()
    login_page = client.get("/login")
    assert login_page.status_code == 200
    token = _csrf(login_page.data)
    logged = client.post(
        "/login",
        data={"email": "admin@local", "password": "admin123", "csrf_token": token},
        follow_redirects=False,
    )
    assert logged.status_code in (302, 303)

    new_page = client.get("/meetings/new")
    assert new_page.status_code == 200
    missing = client.post(
        "/meetings/new",
        data={"name": "CSRF regression", "consent": "1", "client_id": "web-test"},
        follow_redirects=False,
    )
    assert missing.status_code == 400
    assert b"CSRF" in missing.data

    started = client.post(
        "/meetings/new",
        data={
            "name": "CSRF regression",
            "expected_speakers": "4",
            "meeting_type": "tech",
            "consent": "1",
            "client_id": "web-test",
            "csrf_token": _csrf(new_page.data),
        },
        follow_redirects=False,
    )
    assert started.status_code in (302, 303)
    assert "/live" in started.headers["Location"]
