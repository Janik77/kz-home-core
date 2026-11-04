"""Real cross-connection locks/CAS; only an explicit dedicated test database."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from secrets import token_urlsafe
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text, func, event, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, EntityNotFoundError
from app.db import Base
from app.models import (
    HouseORM,
    FloorORM,
    RoomORM,
    UserORM,
    HouseMembershipORM,
    ProvisioningSessionORM,
    PhysicalDeviceORM,
    DeviceORM,
)
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.onboarding import InventoryRegistration
from app.schemas.provisioning import ProvisioningStart
from app.services.onboarding_service import OnboardingService
from app.services.provisioning_service import ProvisioningService


@pytest.fixture
def pg_attempts():
    url = os.getenv("TEST_POSTGRESQL_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRESQL_URL to a dedicated test database")
    if make_url(url).get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRESQL_URL must use PostgreSQL")
    engine = create_engine(url, hide_parameters=True)
    schema = "kzhome_provisioning_test_" + uuid4().hex
    scoped = engine.execution_options(schema_translate_map={None: schema})
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    code = token_urlsafe(32)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config.attributes["connection"] = connection
            command.upgrade(config, "0007_control_authority")
            # Upgrade existing schema and verify no non-provisioning table changes.
            command.upgrade(config, "head")
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
        with Session(scoped) as session:
            for house in ("a", "b"):
                session.add(HouseORM(id=house, name=house))
                session.flush()
                session.add(
                    FloorORM(id="f" + house, house_id=house, name=house, order=0)
                )
                session.flush()
                session.add(RoomORM(id="r" + house, floor_id="f" + house, name=house))
            for actor, role in (
                ("owner", "owner"),
                ("installer", "installer"),
                ("resident", "resident"),
            ):
                session.add(
                    UserORM(
                        id=actor, email=actor + "@example.test", password_hash="unused"
                    )
                )
                session.flush()
                for house in ("a", "b"):
                    session.add(
                        HouseMembershipORM(
                            id=actor + house, user_id=actor, house_id=house, role=role
                        )
                    )
            session.commit()
            OnboardingService(PhysicalDeviceRepository(session)).register_inventory(
                InventoryRegistration(
                    device_id="physical",
                    hardware_id="serial",
                    hardware_model="esp32-relay-v1",
                    claim_code=code,
                )
            )
        yield scoped, code, schema, config
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def body(code, house="a", name="Relay"):
    return ProvisioningStart(
        hardware_id="serial", room_id="r" + house, name=name, claim_code=code
    )


@pytest.mark.parametrize(
    "mode", ["same_key", "different_key", "different_house", "changed_body"]
)
def test_concurrent_create(pg_attempts, mode):
    engine, code, *_ = pg_attempts
    barrier, key = Barrier(2), uuid4()

    def worker(index):
        house = "b" if mode == "different_house" and index else "a"
        with Session(engine) as session:
            session.execute(text("SET lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            try:
                result = ProvisioningService(session).create(
                    house,
                    "owner",
                    body(
                        code,
                        house,
                        "Other" if mode == "changed_body" and index else "Relay",
                    ),
                    uuid4() if mode == "different_key" else key,
                )
                return "ok", result.session_id
            except ConflictError:
                return "conflict", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, (0, 1)))
    assert sum(item[0] == "ok" for item in results) == (2 if mode == "same_key" else 1)
    if mode == "same_key":
        assert results[0][1] == results[1][1]
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ProvisioningSessionORM))
            == 1
        )
        assert session.scalar(select(func.count()).select_from(DeviceORM)) == 0
        assert session.get(PhysicalDeviceORM, "physical").claim_code_hash is not None


def test_cancel_race_and_retry(pg_attempts):
    engine, code, *_ = pg_attempts
    with Session(engine) as session:
        result = ProvisioningService(session).create("a", "owner", body(code), uuid4())
    barrier = Barrier(2)

    def worker(index):
        key = uuid4()
        with Session(engine) as session:
            session.execute(text("SET lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            try:
                service = ProvisioningService(session)
                row = service.cancel("a", "owner", result.session_id, 0, key)
                assert service.cancel("a", "owner", result.session_id, 0, key) == row
                return "cancelled"
            except ConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, (0, 1))) == ["cancelled", "conflict"]
    with Session(engine) as session:
        row = session.get(ProvisioningSessionORM, str(result.session_id))
        assert row.revision == 1 and row.status == "cancelled"


def test_pg_expiry_rbac_isolation_and_unique_index(pg_attempts):
    engine, code, *_ = pg_attempts
    with Session(engine) as session:
        service = ProvisioningService(session)
        result = service.create("a", "owner", body(code), uuid4())
        with pytest.raises(HTTPException) as error:
            service.get("a", "resident", result.session_id)
        assert error.value.status_code == 403
        with pytest.raises(EntityNotFoundError):
            service.get("b", "owner", result.session_id)
        original = session.get(ProvisioningSessionORM, str(result.session_id))
        copied = {
            column.name: getattr(original, column.name)
            for column in original.__table__.columns
        }
        session.rollback()
        copied.update(id=str(uuid4()), create_key_hash="0" * 64)
        session.add(ProvisioningSessionORM(**copied))
        with pytest.raises(IntegrityError):
            session.commit()  # Index protects even a writer bypassing service locks.
        session.rollback()
        from sqlalchemy import update

        session.execute(
            update(ProvisioningSessionORM)
            .where(ProvisioningSessionORM.id == str(result.session_id))
            .values(
                created_at=func.clock_timestamp() - text("interval '10 minutes'"),
                expires_at=func.clock_timestamp() - text("interval '1 second'"),
            )
        )
        session.commit()
        assert service.get("a", "installer", result.session_id).status == "expired"
        assert (
            service.create("a", "installer", body(code), uuid4()).status == "discovered"
        )


def test_downgrade_preserves_history(pg_attempts):
    engine, code, schema, config = pg_attempts
    with Session(engine) as session:
        ProvisioningService(session).create("a", "owner", body(code), uuid4())
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="history is not empty"):
            command.downgrade(config, "0007_control_authority")


def test_sql_cas_rejects_stale_snapshot_even_without_row_lock(pg_attempts):
    engine, code, *_ = pg_attempts
    from app.services.provisioning_state import ProvisioningEvent

    with Session(engine) as first, Session(engine) as second:
        result = ProvisioningService(first).create("a", "owner", body(code), uuid4())
        stale = first.get(ProvisioningSessionORM, str(result.session_id))
        ProvisioningService(second).cancel("a", "owner", result.session_id, 0, uuid4())
        service = ProvisioningService(first)
        with pytest.raises(ConflictError, match="revision conflict"):
            service._apply(stale, ProvisioningEvent.CANCEL, service._now())
        first.rollback()
        assert first.get(ProvisioningSessionORM, str(result.session_id)).revision == 1


def test_create_samples_db_clock_after_waiting_for_active_row(pg_attempts):
    engine, code, *_ = pg_attempts
    with Session(engine) as setup:
        initial = ProvisioningService(setup).create("a", "owner", body(code), uuid4())
        setup.execute(
            update(ProvisioningSessionORM).values(
                created_at=func.clock_timestamp() - text("interval '10 minutes'"),
                expires_at=func.clock_timestamp() - text("interval '1 second'"),
            )
        )
        setup.commit()
    reached = Event()

    def observe(connection, cursor, statement, parameters, context, many):
        if (
            "FOR UPDATE" in statement
            and "WHERE" in statement
            and "provisioning_sessions.device_id =" in statement.split("WHERE", 1)[1]
        ):
            reached.set()

    def create_after_wait():
        with Session(engine) as session:
            session.execute(text("SET lock_timeout = '5s'"))
            result = ProvisioningService(session).create(
                "a", "owner", body(code), uuid4()
            )
            return session.get(
                ProvisioningSessionORM, str(result.session_id)
            ).created_at

    with Session(engine) as blocker:
        blocker.scalar(
            select(ProvisioningSessionORM)
            .where(ProvisioningSessionORM.id == str(initial.session_id))
            .with_for_update()
        )
        event.listen(engine, "before_cursor_execute", observe)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(create_after_wait)
                try:
                    assert reached.wait(timeout=5)
                    released_at = blocker.scalar(select(func.clock_timestamp()))
                finally:
                    blocker.commit()
                assert future.result(timeout=10) >= released_at
        finally:
            event.remove(engine, "before_cursor_execute", observe)
