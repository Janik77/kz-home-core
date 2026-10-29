from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db import Base
from app.models import DeviceCommandORM, HouseORM
from test_command_lifecycle import fixture
import asyncio


@pytest.mark.parametrize("upgrade", [False, True])
def test_command_migration_matches_models_and_preserves_old_data(tmp_path, upgrade):
    engine = create_engine(f"sqlite:///{tmp_path / 'command-migration.db'}")
    try:
        with engine.begin() as connection:
            config = Config()
            config.set_main_option(
                "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
            )
            config.attributes["connection"] = connection
            if upgrade:
                command.upgrade(config, "0005_device_onboarding")
                connection.execute(
                    HouseORM.__table__.insert().values(id="preserve", name="Preserve")
                )
            command.upgrade(config, "head")
            assert (
                connection.scalar(text("select version_num from alembic_version"))
                == "0006_command_lifecycle"
            )
            if upgrade:
                assert (
                    connection.scalar(
                        text("select name from houses where id='preserve'")
                    )
                    == "Preserve"
                )
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_object": lambda obj, name, kind, reflected, compared: kind
                    != "table"
                    or name == "device_commands"
                },
            )
            assert compare_metadata(context, Base.metadata) == []
            assert "uq_command_pending_device" in {
                i["name"] for i in inspect(connection).get_indexes("device_commands")
            }
            command.downgrade(config, "0005_device_onboarding")
            assert "device_commands" not in inspect(connection).get_table_names()
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_database_enforces_single_pending_command_and_retains_history_on_downgrade(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            receipt = (await h.post()).json()
            with Session(h.engine) as session:
                row = session.get(DeviceCommandORM, receipt["command_id"])
                values = {
                    column.name: getattr(row, column.name)
                    for column in DeviceCommandORM.__table__.columns
                }
                values.update(
                    command_id="different",
                    correlation_id="different",
                    idempotency_hash="f" * 64,
                )
                session.add(DeviceCommandORM(**values))
                with pytest.raises(IntegrityError):
                    session.commit()
                session.rollback()
            with h.engine.begin() as connection:
                config = Config()
                config.set_main_option(
                    "script_location",
                    str(Path(__file__).resolve().parents[1] / "alembic"),
                )
                config.attributes["connection"] = connection
                with pytest.raises(RuntimeError, match="command history is not empty"):
                    command.downgrade(config, "0005_device_onboarding")

    asyncio.run(check())
