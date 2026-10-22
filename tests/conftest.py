"""Pytest fixtures.

If DATABASE_URL is unset, this module uses a SQLite test double. Application
code still targets PostgreSQL (postgresql+psycopg dialect) outside tests.
JSON columns stand in for ARRAY / pgvector so both backends work.
FakeLLMProvider is the default. Never call the local Qwen gateway in default pytest.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# A test run must never inherit the desktop service's PostgreSQL connection.
# ``DATABASE_URL`` can be exported by a shell/IDE even though the local .env is
# deliberately not loaded in test mode.  Use an explicitly named test-only
# URL when a persistent test database is genuinely needed; otherwise isolate
# every fixture in SQLite.
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("LLM_BACKEND", "fake")
os.environ.setdefault("SPEECH_BACKEND", "fake")

from app.config import TestConfig
from app.models import User
from app.providers.llm import FakeLLMProvider
from app.web.extensions import db
from app.web.factory import create_app, seed_users


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture()
def app(data_dir: Path):
    uri = os.environ.get("TEST_DATABASE_URL") or "sqlite://"

    class Local(TestConfig):
        SQLALCHEMY_DATABASE_URI = uri
        DATA_DIR = data_dir
        WTF_CSRF_ENABLED = False
        TESTING = True
        LLM_BACKEND = "fake"
        SPEECH_BACKEND = "fake"

    application = create_app(Local, DATA_DIR=str(data_dir), SQLALCHEMY_DATABASE_URI=uri)
    application.extensions["providers"].llm = FakeLLMProvider()
    with application.app_context():
        db.create_all()
        seed_users()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, email: str, password: str):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def auth_admin(client):
    login(client, "admin@local", "admin123")
    return client


@pytest.fixture()
def auth_member(client):
    login(client, "member@local", "member123")
    return client


@pytest.fixture()
def auth_viewer(client):
    login(client, "viewer@local", "viewer123")
    return client
