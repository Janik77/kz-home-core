"""Fresh/upgrade/empty-downgrade on SQLite; PostgreSQL parity is checked separately."""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.db import Base
import app.models  # noqa: F401


@pytest.mark.parametrize("fresh", [True, False])
def test_migration_roundtrip_and_preservation(tmp_path, fresh):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            if not fresh:
                command.upgrade(config, "0007_control_authority")
                connection.execute(
                    text(
                        "INSERT INTO houses (id, name) VALUES ('preserved', 'Preserved')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO physical_devices (device_id, hardware_id, hardware_model, protocol_version, status, claim_code_hash) VALUES ('physical', 'serial', 'esp32-relay-v1', 'v1', 'unprovisioned', :digest)"
                    ),
                    {"digest": "a" * 64},
                )
            command.upgrade(config, "head")
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0008_provisioning_sessions"
            )
            differences = compare_metadata(
                MigrationContext.configure(
                    connection,
                    opts={
                        "include_object": lambda obj,
                        name,
                        kind,
                        reflected,
                        compared: kind != "table" or name == "provisioning_sessions",
                    },
                ),
                Base.metadata,
            )
            assert differences == []
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM provisioning_sessions"))
                == 0
            )
            if not fresh:
                assert (
                    connection.scalar(
                        text("SELECT name FROM houses WHERE id = 'preserved'")
                    )
                    == "Preserved"
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT claim_code_hash FROM physical_devices WHERE device_id = 'physical'"
                        )
                    )
                    == "a" * 64
                )
            command.downgrade(config, "0007_control_authority")
            assert "provisioning_sessions" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
