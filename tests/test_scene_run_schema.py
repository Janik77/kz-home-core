from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from app.db import Base
from test_schema import migration_config


def test_scene_run_schema_matches_models_and_empty_roundtrip(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scene-schema.db'}")
    try:
        with engine.begin() as connection:
            config = migration_config(connection)
            command.upgrade(config, "head")
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_object": lambda obj, name, kind, reflected, compared: kind
                    != "table"
                    or name.startswith("scene_run")
                },
            )
            assert compare_metadata(context, Base.metadata) == []
            command.downgrade(config, "0009_definition_revisions")
            assert "scene_runs" not in inspect(connection).get_table_names()
            command.upgrade(config, "head")
            assert "scene_run_receipts" in inspect(connection).get_table_names()
    finally:
        engine.dispose()
