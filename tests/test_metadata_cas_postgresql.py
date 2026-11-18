# ruff: noqa: F811
"""Actual SQL CAS contention; dedicated CI PostgreSQL only."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.core.errors import MetadataRevisionConflict
from app.models import HouseORM, FloorORM, RoomORM, DeviceORM
from app.repositories import (
    HouseRepository,
    FloorRepository,
    RoomRepository,
    DeviceRepository,
)
from test_definition_postgresql import pg_definitions  # noqa: F401


@pytest.mark.parametrize(
    "repository,model,identifier",
    [
        (HouseRepository, HouseORM, "a"),
        (FloorRepository, FloorORM, "floor"),
        (RoomRepository, RoomORM, "room"),
        (DeviceRepository, DeviceORM, "fixture"),
    ],
)
def test_two_metadata_editors_have_one_atomic_winner(
    pg_definitions, repository, model, identifier
):
    barrier = Barrier(2)

    def edit(index):
        with Session(pg_definitions, expire_on_commit=False) as session:
            session.execute(text("SET lock_timeout = '5s'"))
            repo = repository(session)
            original = repo.get(identifier)
            assert original.metadata_revision == 1
            barrier.wait(timeout=5)
            try:
                saved = repo.update(
                    identifier,
                    {"name": f"Editor {index}", "expected_metadata_revision": 1},
                )
                return "saved", saved.name, saved.metadata_revision
            except MetadataRevisionConflict:
                return "conflict", None, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, [0, 1]))
    assert sorted(x[0] for x in results) == ["conflict", "saved"]
    winner = next(x for x in results if x[0] == "saved")
    assert winner[2] == 2
    with Session(pg_definitions) as session:
        current = session.get(model, identifier)
        assert current.metadata_revision == 2 and current.name == winner[1]


@pytest.mark.parametrize("operation", ["rename_room", "create_room", "create_device"])
@pytest.mark.parametrize("race", ["parent_transfer", "revoke", "deactivate"])
def test_blocked_postgres_scope_rechecks_committed_changes(
    pg_definitions, monkeypatch, operation, race
):
    """A real second transaction holds the house lock while the HTTP edit waits."""
    from threading import Event
    from fastapi.testclient import TestClient
    from app.core import Settings
    from app.models import UserORM, HouseMembershipORM, DeviceCommandORM
    from app.repositories.physical_device_repository import PhysicalDeviceRepository
    from app.security import TokenCodec
    from sqlalchemy import select
    import app.main as main

    with Session(pg_definitions) as db:
        db.add(
            UserORM(
                id="metadata-user",
                email="metadata@example.test",
                password_hash="unused",
            )
        )
        db.flush()
        db.add(
            HouseMembershipORM(
                id="metadata-member",
                user_id="metadata-user",
                house_id="a",
                role="owner",
            )
        )
        db.commit()
    settings = Settings(
        database_url=str(pg_definitions.url), app_env="test", mqtt_enabled=False
    )
    monkeypatch.setattr(main, "create_db_engine", lambda _: pg_definitions)
    codec = TokenCodec(
        settings.auth_jwt_secret,
        settings.auth_jwt_algorithm,
        settings.auth_access_token_minutes,
        settings.auth_refresh_token_days,
    )
    token, _ = codec.issue("metadata-user", "access")
    from uuid import uuid4
    import time

    marker = "kz_metadata_scope_" + uuid4().hex
    entered = Event()
    original = PhysicalDeviceRepository.lock_houses

    def waited(repository, houses):
        repository.session.execute(text("SET LOCAL lock_timeout = '8s'"))
        repository.session.execute(
            text("SELECT set_config('application_name', :marker, true)"),
            {"marker": marker},
        )
        entered.set()
        return original(repository, houses)

    monkeypatch.setattr(PhysicalDeviceRepository, "lock_houses", waited)
    with TestClient(main.create_app(settings, run_simulator=False)) as client:
        client.headers["Authorization"] = f"Bearer {token}"

        def edit():
            if operation == "rename_room":
                return client.patch(
                    "/rooms/room",
                    json={"name": "must not save", "expected_metadata_revision": 1},
                )
            if operation == "create_room":
                return client.post(
                    "/rooms",
                    json={
                        "id": "race-child",
                        "floor_id": "floor",
                        "name": "must not save",
                    },
                )
            return client.post(
                "/devices",
                json={
                    "id": "race-child",
                    "room_id": "room",
                    "name": "must not save",
                    "type": "relay",
                    "capabilities": ["on_off"],
                    "state": {"on": False},
                },
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            with Session(pg_definitions) as blocker:
                blocker.execute(text("SET lock_timeout = '5s'"))
                blocker.scalar(
                    select(HouseORM).where(HouseORM.id == "a").with_for_update()
                )
                future = pool.submit(edit)
                assert entered.wait(timeout=5), (
                    "HTTP edit did not reach the held house lock"
                )
                assert not future.done(), (
                    "Edit unexpectedly bypassed the held house lock"
                )
                deadline = time.monotonic() + 5
                while True:
                    blocker.execute(text("SELECT pg_stat_clear_snapshot()"))
                    blocked = blocker.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE application_name=:marker AND wait_event_type='Lock'"
                        ),
                        {"marker": marker},
                    )
                    if blocked:
                        break
                    assert not future.done() and time.monotonic() < deadline, (
                        "HTTP edit never demonstrably waited on the actual PostgreSQL lock"
                    )
                    time.sleep(
                        0.01
                    )  # Poll observed DB wait state, not assumed race timing.
                if race == "parent_transfer":
                    blocker.get(FloorORM, "floor").house_id = "b"
                elif race == "revoke":
                    blocker.get(HouseMembershipORM, "metadata-member").role = "resident"
                else:
                    blocker.get(UserORM, "metadata-user").is_active = False
                blocker.commit()
            response = future.result(timeout=10)
        assert (
            response.status_code
            == {"parent_transfer": 409, "revoke": 403, "deactivate": 401}[race]
        ), response.text
        with Session(pg_definitions) as db:
            room = db.get(RoomORM, "room")
            assert room.name == "Room" and room.metadata_revision == 1
            assert db.get(RoomORM, "race-child") is None
            assert db.get(DeviceORM, "race-child") is None
            assert db.scalar(select(DeviceCommandORM)) is None
