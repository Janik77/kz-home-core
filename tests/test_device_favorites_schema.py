import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from app.db import Base
from test_schema import migration_config


def test_favorites_additive_migration_preserves_data_and_empty_roundtrip(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            config = migration_config(connection)
            command.upgrade(config, "0010_scene_runs")
            connection.execute(
                text("INSERT INTO houses(id,name) VALUES ('saved','Keep me')")
            )
            command.upgrade(config, "head")
            assert (
                connection.scalar(text("SELECT name FROM houses WHERE id='saved'"))
                == "Keep me"
            )
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_object": lambda obj, name, kind, reflected, compared: kind
                    != "table"
                    or name == "device_favorites"
                },
            )
            assert compare_metadata(context, Base.metadata) == []
            assert connection.scalar(text("SELECT count(*) FROM device_favorites")) == 0
            command.downgrade(config, "0010_scene_runs")
            assert "device_favorites" not in inspect(connection).get_table_names()
            assert (
                connection.scalar(text("SELECT name FROM houses WHERE id='saved'"))
                == "Keep me"
            )
            command.upgrade(config, "head")
            # Isolated SQLite fixture enables no FKs; this artificial row tests
            # only downgrade's guard, not referential integrity (covered in PG).
            connection.execute(
                text("INSERT INTO device_favorites VALUES ('u','saved','d')")
            )
            with pytest.raises(RuntimeError, match="refusing preference loss"):
                command.downgrade(config, "0010_scene_runs")
            assert connection.scalar(text("SELECT count(*) FROM device_favorites")) == 1
    finally:
        engine.dispose()
