import json
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from app.db import Base
from test_schema import migration_config


@pytest.mark.parametrize("kind", ["legacy", "scene", "automation"])
def test_wait_additive_and_definition_downgrade_guard(tmp_path, kind):
    engine = create_engine(f"sqlite:///{tmp_path / 'wait-migration.db'}")
    with engine.begin() as conn:
        config = migration_config(conn)
        command.upgrade(config, "0015_house_schedules")
        conn.execute(text("INSERT INTO houses(id,name) VALUES ('h','Original')"))
        legacy = [{"type": "delay", "seconds": 9}]
        conn.execute(
            text(
                "INSERT INTO automations(id,name,house_id,enabled,trigger,conditions,actions) VALUES ('a','Legacy','h',false,'{}','[]',:actions)"
            ),
            {"actions": json.dumps(legacy)},
        )
        command.upgrade(config, "head")
        assert (
            compare_metadata(
                MigrationContext.configure(
                    conn,
                    opts={
                        "include_object": lambda obj,
                        name,
                        kind,
                        reflected,
                        compared: kind != "table"
                        or name
                        in {
                            "scene_runs",
                            "scene_run_actions",
                            "scene_run_waits",
                            "houses",
                            "automations",
                        }
                    },
                ),
                Base.metadata,
            )
            == []
        )
        assert (
            json.loads(conn.scalar(text("SELECT actions FROM automations"))) == legacy
        )
        if kind == "legacy":
            command.downgrade(config, "0015_house_schedules")
            assert (
                json.loads(conn.scalar(text("SELECT actions FROM automations")))
                == legacy
            )
        else:
            value = json.dumps([{"type": "durable_wait", "seconds": 1}])
            if kind == "scene":
                conn.execute(
                    text(
                        "INSERT INTO scenes(id,name,house_id,actions) VALUES ('s','Draft','h',:actions)"
                    ),
                    {"actions": value},
                )
            else:
                conn.execute(
                    text("UPDATE automations SET actions=:actions"), {"actions": value}
                )
            with pytest.raises(RuntimeError, match="restore a verified backup"):
                command.downgrade(config, "0015_house_schedules")
            assert (
                conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "0016_durable_waits"
            )
    engine.dispose()
