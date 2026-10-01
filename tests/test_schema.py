"""Migration checks; PostgreSQL is opt-in via TEST_POSTGRESQL_URL.

Use a dedicated test database (postgresql+psycopg://...). The PostgreSQL test
creates a random isolated schema inside a transaction and rolls it back.
It never falls back to DATABASE_URL or loads credentials from .env.
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError

from app.models import EventLogORM
from app.transports.mqtt_models import StateEnvelope

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0004_event_log_correlation"
ONBOARDING_HEAD = "0005_device_onboarding"
PREVIOUS = "0003_auth_foundation"


def migration_config(connection=None):
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def test_migration_chain_has_one_head():
    scripts = ScriptDirectory.from_config(migration_config())
    assert scripts.get_heads() == [ONBOARDING_HEAD]
    revisions = list(scripts.walk_revisions())
    assert [revision.revision for revision in revisions] == [
        ONBOARDING_HEAD,
        HEAD,
        PREVIOUS,
        "0002_automation_engine",
        "0001_initial",
    ]
    assert [revision.down_revision for revision in revisions] == [
        HEAD,
        PREVIOUS,
        "0002_automation_engine",
        "0001_initial",
        None,
    ]


def assert_column(connection, length):
    column = next(
        c
        for c in inspect(connection).get_columns("event_logs")
        if c["name"] == "correlation_id"
    )
    assert column["type"].length == length
    assert column["nullable"] is False
    assert "ix_event_logs_correlation_id" in {
        index["name"] for index in inspect(connection).get_indexes("event_logs")
    }


def insert_event(connection, correlation_id):
    event_id = str(uuid4())
    connection.execute(
        EventLogORM.__table__.insert().values(
            id=event_id,
            event_type="device_state_changed",
            payload={},
            correlation_id=correlation_id,
        )
    )
    return event_id


@pytest.mark.parametrize("from_previous", [False, True])
def test_sqlite_migrations_preserve_ids_and_support_protocol_limit(
    tmp_path, from_previous
):
    engine = create_engine(f"sqlite:///{(tmp_path / 'schema.db').as_posix()}")
    try:
        with engine.begin() as connection:
            verify_upgrade(connection, from_previous)
    finally:
        engine.dispose()


def verify_upgrade(connection, from_previous):
    config = migration_config(connection)
    old_id = None
    if from_previous:
        command.upgrade(config, PREVIOUS)
        assert_column(connection, 36)
        old_id = insert_event(connection, "a" * 36)
    command.upgrade(config, "head")
    assert (
        connection.scalar(text("SELECT version_num FROM alembic_version"))
        == ONBOARDING_HEAD
    )
    assert_column(connection, 128)
    assert EventLogORM.__table__.c.correlation_id.type.length == 128
    if old_id:
        assert (
            connection.scalar(
                select(EventLogORM.correlation_id).where(EventLogORM.id == old_id)
            )
            == "a" * 36
        )
    envelope = StateEnvelope(
        timestamp="2026-09-25T00:00:00Z", correlation_id="c" * 128, state={"on": True}
    )
    event_id = insert_event(connection, envelope.correlation_id)
    assert (
        connection.scalar(
            select(EventLogORM.correlation_id).where(EventLogORM.id == event_id)
        )
        == envelope.correlation_id
    )
    with pytest.raises(RuntimeError, match="longer than 36"):
        command.downgrade(config, PREVIOUS)
    assert_column(connection, 128)
    connection.execute(EventLogORM.__table__.delete().where(EventLogORM.id == event_id))
    command.downgrade(config, PREVIOUS)
    assert_column(connection, 36)
    command.upgrade(config, "head")
    assert_column(connection, 128)


@pytest.mark.parametrize("from_previous", [False, True])
def test_postgresql_migrations_and_length_enforcement(from_previous):
    url = os.getenv("TEST_POSTGRESQL_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRESQL_URL to a dedicated PostgreSQL test database")
    if make_url(url).get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRESQL_URL must use PostgreSQL")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                schema = "kzhome_test_" + uuid4().hex
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                verify_upgrade(connection, from_previous)
                with pytest.raises(DataError):
                    with connection.begin_nested():
                        insert_event(connection, "x" * 129)
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
