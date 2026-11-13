"""Upgrade preserves definitions and backfills revisions; downgrade removes only counters."""

import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from app.db import Base
import app.models  # noqa: F401


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_definition_revision_migration_preserves_rows(tmp_path, backend):
    url = (
        os.getenv("TEST_POSTGRESQL_URL")
        if backend == "postgresql"
        else f"sqlite:///{tmp_path / 'definitions.db'}"
    )
    if not url:
        pytest.skip("Set TEST_POSTGRESQL_URL to a dedicated PostgreSQL test database")
    if backend == "postgresql" and make_url(url).get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRESQL_URL must use PostgreSQL")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                if backend == "postgresql":
                    schema = "kz_definition_migration_" + uuid4().hex
                    conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                    conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                config = Config()
                config.set_main_option(
                    "script_location",
                    str(Path(__file__).resolve().parents[1] / "alembic"),
                )
                config.attributes["connection"] = conn
                command.upgrade(config, "0008_provisioning_sessions")
                metadata = MetaData()
                house = Table("houses", metadata, autoload_with=conn)
                scene = Table("scenes", metadata, autoload_with=conn)
                rule = Table("automations", metadata, autoload_with=conn)
                conn.execute(house.insert().values(id="home", name="Home"))
                conn.execute(
                    scene.insert().values(
                        id="scene", house_id="home", name="Scene", actions=[]
                    )
                )
                conn.execute(
                    rule.insert().values(
                        id="rule",
                        house_id="home",
                        name="Rule",
                        enabled=False,
                        trigger={"device_id": "fixture"},
                        conditions=[],
                        actions=[{"type": "delay", "seconds": 1}],
                    )
                )
                before = {
                    table.name: dict(conn.execute(select(table)).mappings().one())
                    for table in (scene, rule)
                }
                command.upgrade(config, "head")
                for name, original in before.items():
                    table = Table(name, MetaData(), autoload_with=conn)
                    row = dict(conn.execute(select(table)).mappings().one())
                    assert row.pop("revision") == 1
                    assert row == original
                    column = next(
                        c
                        for c in inspect(conn).get_columns(name)
                        if c["name"] == "revision"
                    )
                    assert not column["nullable"]
                context = MigrationContext.configure(
                    conn,
                    opts={
                        "include_object": lambda obj,
                        name,
                        kind,
                        reflected,
                        compared: kind != "table" or name in {"scenes", "automations"}
                    },
                )
                assert compare_metadata(context, Base.metadata) == []
                command.downgrade(config, "0008_provisioning_sessions")
                for table in (scene, rule):
                    assert "revision" not in {
                        c["name"] for c in inspect(conn).get_columns(table.name)
                    }
                    assert (
                        dict(conn.execute(select(table)).mappings().one())
                        == before[table.name]
                    )
                command.upgrade(config, "head")
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
