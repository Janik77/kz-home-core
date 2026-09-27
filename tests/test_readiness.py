from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError

from app.core import Settings


@pytest.fixture
def readiness_app(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    import app.main as main

    engine = create_engine(f"sqlite:///{(tmp_path / 'ready.db').as_posix()}")
    monkeypatch.setattr(main, "create_db_engine", lambda settings: engine)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )

    def migrate(revision):
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)

    app = main.create_app(Settings(database_url=str(engine.url), app_env="test"))
    try:
        yield app, engine, migrate
    finally:
        engine.dispose()


def test_healthy_readiness_and_lifespan(readiness_app):
    app, engine, migrate = readiness_app
    migrate("head")
    with TestClient(app) as client:
        for _ in range(3):
            response = client.get("/ready")
            assert response.status_code == 200
            assert response.json() == {"status": "ready"}
            assert response.headers["cache-control"] == "no-store"
            assert engine.pool.checkedout() == 0
        assert client.get("/health").json() == {"status": "ok", "version": "0.6.0b1"}
    assert app.state.initialized is False


def test_readiness_without_lifespan_does_not_connect(readiness_app, monkeypatch):
    app, engine, _ = readiness_app

    def unexpected_connect():
        pytest.fail("Uninitialized readiness must not open a connection")

    monkeypatch.setattr(engine, "connect", unexpected_connect)
    client = TestClient(app)
    try:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["reason"] == "application_not_initialized"
    finally:
        client.close()


def test_behind_schema_is_not_migrated_by_probe(readiness_app):
    app, engine, migrate = readiness_app
    migrate("0003_auth_foundation")
    with TestClient(app) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["reason"] == "schema_revision_mismatch"
        assert client.get("/health").status_code == 200
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0003_auth_foundation"
        )


def test_missing_schema_is_not_created(readiness_app):
    app, engine, _ = readiness_app
    with TestClient(app) as client:
        assert client.get("/ready").json()["reason"] == "schema_revision_mismatch"
        assert inspect(engine).get_table_names() == []


@pytest.mark.parametrize(
    "versions",
    [[], ["unknown_revision"], ["0004_event_log_correlation", "extra_revision"]],
)
def test_unexpected_database_revisions_fail_closed(readiness_app, versions):
    app, engine, migrate = readiness_app
    migrate("head")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
        for version in versions:
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:version)"),
                {"version": version},
            )
    with TestClient(app) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "not_ready",
            "reason": "schema_revision_mismatch",
        }


@pytest.mark.parametrize("stage", ["connect", "query"])
def test_database_failure_is_safe_and_connections_are_released(
    readiness_app, monkeypatch, caplog, stage
):
    app, engine, migrate = readiness_app
    migrate("head")
    secret = "synthetic-credential-must-not-leak"

    def unavailable(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, RuntimeError(secret))

    with TestClient(app) as client:
        with monkeypatch.context() as patch:
            if stage == "connect":
                patch.setattr(engine, "connect", unavailable)
            else:
                event.listen(engine, "before_cursor_execute", unavailable)
            try:
                response = client.get("/ready")
                assert response.status_code == 503
                assert response.json() == {
                    "status": "not_ready",
                    "reason": "database_unavailable",
                }
                assert secret not in response.text
                assert secret not in caplog.text
                assert engine.pool.checkedout() == 0
                assert client.get("/health").json() == {
                    "status": "ok",
                    "version": "0.6.0b1",
                }
            finally:
                if stage == "query":
                    event.remove(engine, "before_cursor_execute", unavailable)
        assert client.get("/ready").status_code == 200
