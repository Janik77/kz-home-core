"""Fresh/upgrade schema checks and downgrade protection for physical identities."""

from pathlib import Path
from secrets import token_urlsafe

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import Session

from app.db import Base
from app.models import DeviceORM, FloorORM, HouseORM, PhysicalDeviceORM, RoomORM
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.onboarding import DeviceClaim, InventoryRegistration
from app.services.onboarding_service import OnboardingService

HEAD = "0005_device_onboarding"
PREVIOUS = "0004_event_log_correlation"


def config_for(connection):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    config.attributes["connection"] = connection
    return config


@pytest.mark.parametrize("fresh", [True, False])
def test_onboarding_migration_preserves_existing_devices_and_matches_models(
    tmp_path, fresh
):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        with engine.begin() as connection:
            config = config_for(connection)
            if not fresh:
                command.upgrade(config, PREVIOUS)
                connection.execute(
                    HouseORM.__table__.insert().values(id="house", name="House")
                )
                connection.execute(
                    FloorORM.__table__.insert().values(
                        id="floor", house_id="house", name="Floor", order=0
                    )
                )
                connection.execute(
                    RoomORM.__table__.insert().values(
                        id="room", floor_id="floor", name="Room"
                    )
                )
                connection.execute(
                    DeviceORM.__table__.insert().values(
                        id="legacy",
                        room_id="room",
                        name="Legacy",
                        type="relay",
                        state={"on": True},
                        online=True,
                        capabilities=["on_off"],
                        metadata={},
                    )
                )
            command.upgrade(config, "head")
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == HEAD
            )
            # Existing user-email migration has a redundant unique constraint;
            # inspect only this revision's table rather than changing old schema.
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_object": lambda obj, name, kind, reflected, compared: kind
                    != "table"
                    or name == "physical_devices",
                },
            )
            assert compare_metadata(context, Base.metadata) == []
            assert (
                connection.scalar(
                    select(text("count(*)")).select_from(PhysicalDeviceORM)
                )
                == 0
            )
            if not fresh:
                assert connection.scalar(
                    select(DeviceORM.state).where(DeviceORM.id == "legacy")
                ) == {"on": True}
            command.downgrade(config, PREVIOUS)
            assert "physical_devices" not in inspect(connection).get_table_names()
            command.upgrade(config, HEAD)
    finally:
        engine.dispose()


def test_downgrade_refuses_to_erase_inventory_or_revocation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'protected.db'}")
    try:
        with engine.begin() as connection:
            command.upgrade(config_for(connection), HEAD)
        code = token_urlsafe(32)
        with Session(engine) as session:
            service = OnboardingService(PhysicalDeviceRepository(session))
            service.register_inventory(
                InventoryRegistration(
                    device_id="physical",
                    hardware_id="serial",
                    hardware_model="esp32-c6-relay-v1",
                    claim_code=code,
                )
            )
        with engine.begin() as connection:
            with pytest.raises(RuntimeError, match="inventory is not empty"):
                command.downgrade(config_for(connection), PREVIOUS)
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == HEAD
            )
        with Session(engine) as session:
            session.add(HouseORM(id="house", name="House"))
            session.flush()
            session.add(FloorORM(id="floor", house_id="house", name="Floor", order=0))
            session.flush()
            session.add(RoomORM(id="room", floor_id="floor", name="Room"))
            session.commit()
            service = OnboardingService(PhysicalDeviceRepository(session))
            service.claim(
                "house",
                DeviceClaim(
                    hardware_id="serial", room_id="room", name="Relay", claim_code=code
                ),
                "operator",
            )
            service.transition("house", "physical", "revoked", "operator")
        with engine.begin() as connection:
            with pytest.raises(RuntimeError, match="inventory is not empty"):
                command.downgrade(config_for(connection), PREVIOUS)
            assert connection.scalar(select(PhysicalDeviceORM.status)) == "revoked"
            assert connection.scalar(select(PhysicalDeviceORM.house_id)) == "house"
    finally:
        engine.dispose()


def test_offline_downgrade_refuses_unchecked_inventory():
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://unused/unused")
    # Avoid Settings/local configuration in the offline environment.
    config.attributes["connection"] = object()
    with pytest.raises(RuntimeError, match="online data check"):
        command.downgrade(config, f"{HEAD}:{PREVIOUS}", sql=True)
