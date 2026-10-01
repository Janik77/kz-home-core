"""Opt-in production row-lock checks in a disposable, uniquely named schema.

Requires TEST_POSTGRESQL_URL pointing to a dedicated test database, never .env
or DATABASE_URL. These checks commit only their isolated schema and remove it
on completion because independent concurrent transactions must see the setup.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from secrets import token_urlsafe
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, EntityNotFoundError
from app.models import (
    DeviceORM,
    EventLogORM,
    FloorORM,
    HouseORM,
    PhysicalDeviceORM,
    RoomORM,
)
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.onboarding import DeviceClaim, InventoryRegistration
from app.services.onboarding_service import OnboardingService


@pytest.mark.parametrize("same_house", [True, False])
def test_postgresql_concurrent_claim_has_exactly_one_binding(same_house):
    url = os.getenv("TEST_POSTGRESQL_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRESQL_URL to a dedicated PostgreSQL test database")
    if make_url(url).get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRESQL_URL must use PostgreSQL")
    engine = create_engine(url)
    schema = "kzhome_onboarding_test_" + uuid4().hex
    scoped = engine.execution_options(schema_translate_map={None: schema})
    code = token_urlsafe(32)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config()
            config.set_main_option(
                "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
            )
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with Session(scoped) as session:
            for house in ("a", "b"):
                session.add(HouseORM(id=house, name=house))
                session.flush()
                session.add(
                    FloorORM(id="f" + house, house_id=house, name=house, order=0)
                )
                session.flush()
                session.add(RoomORM(id="r" + house, floor_id="f" + house, name=house))
            session.commit()
            OnboardingService(PhysicalDeviceRepository(session)).register_inventory(
                InventoryRegistration(
                    device_id="physical",
                    hardware_id="serial",
                    hardware_model="esp32-c6-relay-v1",
                    claim_code=code,
                )
            )
        barrier = Barrier(2)

        def claim(house):
            with Session(scoped) as session:
                # Test failures must release locks rather than leave a hanging suite.
                session.execute(text("SET lock_timeout = '5s'"))
                barrier.wait(timeout=5)
                try:
                    result = OnboardingService(PhysicalDeviceRepository(session)).claim(
                        house,
                        DeviceClaim(
                            hardware_id="serial",
                            room_id="r" + house,
                            name="Relay",
                            claim_code=code,
                        ),
                        "operator",
                    )
                    return "claimed", result.house_id
                except (ConflictError, EntityNotFoundError) as error:
                    return type(error).__name__, house

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(claim, ("a", "a" if same_house else "b")))
        assert sum(status == "claimed" for status, _ in results) == 1
        assert (
            sum(
                status == ("ConflictError" if same_house else "EntityNotFoundError")
                for status, _ in results
            )
            == 1
        )
        with Session(scoped) as session:
            binding = session.get(PhysicalDeviceORM, "physical")
            assert binding.status == "provisioning" and binding.claim_code_hash is None
            assert binding.house_id == next(
                house for status, house in results if status == "claimed"
            )
            for model in (DeviceORM, PhysicalDeviceORM, EventLogORM):
                assert session.scalar(select(func.count()).select_from(model)) == 1
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
