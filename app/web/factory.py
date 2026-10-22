"""Flask application factory."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from flask import Flask
from sqlalchemy.pool import StaticPool
from sqlalchemy import inspect, text

from app.config import Config, TestConfig, get_config
from app.logging import configure_logging
from app.providers.registry import build_registry
from app.storage.audio_store import AudioStore
from app.web.extensions import csrf, db, login_manager, socketio

# Windows' MIME registry can map .js to text/plain. ES modules and
# AudioWorklet.addModule require a JavaScript MIME type or browsers reject the
# live client before it can establish its Socket.IO session.
mimetypes.add_type("text/javascript", ".js", strict=True)


def create_app(config_object: type[Config] | Config | None = None, **overrides) -> Flask:
    configure_logging()
    root = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
        static_url_path="/static",
    )
    cfg = config_object or get_config()
    if isinstance(cfg, type):
        app.config.from_object(cfg)
    else:
        app.config.from_object(cfg)
    app.config.update(overrides)

    uri = app.config.get("SQLALCHEMY_DATABASE_URI") or "sqlite://"
    engine_opts = {}
    if uri.startswith("sqlite"):
        engine_opts = {
            "connect_args": {"check_same_thread": False},
        }
        if uri in {"sqlite://", "sqlite:///:memory:"}:
            engine_opts["poolclass"] = StaticPool
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_opts

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    socketio.init_app(app, async_mode="threading", cors_allowed_origins="*")

    data_dir = Path(app.config.get("DATA_DIR") or "./data")
    data_dir.mkdir(parents=True, exist_ok=True)
    app.extensions["audio_store"] = AudioStore(data_dir)
    app.extensions["providers"] = build_registry(app.config)

    from app.models.entities import User  # noqa: F401  register models
    from app.web.auth import load_user  # noqa: F401
    from app.web.blueprints.api import api_bp
    from app.web.blueprints.health import health_bp
    from app.web.blueprints.knowledge import knowledge_bp
    from app.web.blueprints.meetings import meetings_bp
    from app.web.blueprints.members import members_bp
    from app.web.blueprints.pages import pages_bp
    from app.web.blueprints.settings import settings_bp
    from app.web.blueprints.tasks import tasks_bp
    from app.web.blueprints.terminology import terminology_bp
    from app.web.socket_events import register_socketio

    app.register_blueprint(pages_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(knowledge_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(terminology_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(api_bp)

    register_socketio(socketio)

    with app.app_context():
        _initialize_database(app)

    @app.context_processor
    def inject_globals():
        from flask_login import current_user

        return {"current_user": current_user}

    return app


def _initialize_database(app: Flask) -> None:
    """Create/upgrade the schema exactly once during a concurrent cold start.

    The web, speech and task processes all use the application factory.  On a
    fresh PostgreSQL database, their simultaneous ``create_all()`` calls can
    race while SQLAlchemy creates PostgreSQL enum types.  A session advisory
    lock keeps the complete initialization sequence single-filed; SQLite keeps
    its lightweight local behavior.
    """
    uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    locked = uri.startswith("postgresql")
    if not locked:
        db.create_all()
        _upgrade_transcript_columns()
        if not app.config.get("TESTING"):
            seed_users()
        return

    # ``db.session.commit()`` can return the connection carrying a session
    # advisory lock to the pool.  A later unlock may then run on another
    # connection and strand the original lock forever, stopping every speech
    # worker at startup.  A transaction-scoped lock and one explicit DDL
    # connection have deterministic lifetime instead.
    with db.engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 90101701})
        db.metadata.create_all(bind=conn)
        _upgrade_transcript_columns(conn)
    # ``create_all`` above is deliberately committed before using the scoped
    # ORM session.  A newly created PostgreSQL schema is invisible to that
    # separate session until the DDL transaction commits; seeding inside the
    # advisory-lock transaction made a recovered/empty database fail its very
    # first startup with "relation roles does not exist".
    if not app.config.get("TESTING"):
        seed_users()


def seed_users(*, commit: bool = True) -> None:
    from app.models import Role, User
    from app.models.enums import RoleName

    for name, desc in (
        (RoleName.ADMIN.value, "系统管理员"),
        (RoleName.MEMBER.value, "团队成员"),
        (RoleName.VIEWER.value, "只读访客"),
    ):
        if not Role.query.filter_by(name=name).first():
            db.session.add(Role(name=name, description=desc))

    seeds = [
        ("admin@local", "admin123", "管理员", RoleName.ADMIN.value),
        ("member@local", "member123", "成员", RoleName.MEMBER.value),
        ("viewer@local", "viewer123", "访客", RoleName.VIEWER.value),
    ]
    for email, password, uname, role in seeds:
        if not User.query.filter_by(email=email).first():
            u = User(email=email, name=uname, role=role)
            u.set_password(password)
            db.session.add(u)
    # Cold-start initialization keeps the schema lock and seed transaction on
    # one connection; normal callers retain the historical commit behavior.
    if commit:
        db.session.commit()
    else:
        db.session.flush()


def _upgrade_transcript_columns(conn=None) -> None:
    """Apply small additive compatibility upgrades for existing local installs.

    This project deliberately has no migration runtime dependency.  These fields
    are nullable/defaulted and are added only when absent, so the operation is
    safe for the existing local PostgreSQL/SQLite database.
    """
    inspector = inspect(conn or db.engine)
    tables = set(inspector.get_table_names())
    changes: list[tuple[str, str, str]] = []
    if "transcript_segments" in tables:
        existing = {column["name"] for column in inspector.get_columns("transcript_segments")}
        for name, ddl in {
            "punctuated_text": "TEXT",
            "sensevoice_text": "TEXT",
            "final_text": "TEXT",
            "asr_state": "VARCHAR(16) DEFAULT 'final'",
            "asr_revision": "INTEGER DEFAULT 0",
            "terminology_snapshot_id": "VARCHAR(36)",
        }.items():
            if name not in existing:
                changes.append(("transcript_segments", name, ddl))
    if "knowledge_documents" in tables:
        existing = {column["name"] for column in inspector.get_columns("knowledge_documents")}
        if "source_meeting_id" not in existing:
            changes.append(("knowledge_documents", "source_meeting_id", "VARCHAR(36)"))
    if "meeting_import_jobs" in tables:
        existing = {column["name"] for column in inspector.get_columns("meeting_import_jobs")}
        if "kind" not in existing:
            changes.append(("meeting_import_jobs", "kind", "VARCHAR(16) DEFAULT 'import'"))
    if conn is not None:
        for table, name, ddl in changes:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        return
    with db.engine.begin() as connection:
        for table, name, ddl in changes:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def create_test_app(**overrides) -> Flask:
    return create_app(TestConfig, **overrides)
