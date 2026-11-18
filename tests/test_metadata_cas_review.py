# ruff: noqa: F811
"""Independent deterministic scope-lock boundary tests; SQLite, not PG contention."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models import HouseORM, FloorORM, RoomORM, UserORM, HouseMembershipORM
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from tests.test_api import client, db_settings, seeded_client  # noqa: F401


@pytest.mark.parametrize("operation", ["rename_room", "create_room", "create_device"])
@pytest.mark.parametrize("race", ["parent_transfer", "revoke", "deactivate"])
def test_scope_is_revalidated_after_lock_boundary(
    seeded_client, db_settings, monkeypatch, operation, race
):
    room = seeded_client.get("/rooms").json()[0]
    floor = seeded_client.get("/floors/" + room["floor_id"]).json()
    engine = create_engine(db_settings.database_url)
    original = PhysicalDeviceRepository.lock_houses
    hit = False

    def boundary(repository, houses):
        nonlocal hit
        if not hit:
            hit = True
            with Session(engine) as other:
                user = other.scalar(
                    select(UserORM).where(UserORM.email == "owner@example.test")
                )
                if race == "parent_transfer":
                    other.add(HouseORM(id="review-foreign", name="Other"))
                    other.flush()
                    other.get(FloorORM, floor["id"]).house_id = "review-foreign"
                elif race == "revoke":
                    membership = other.scalar(
                        select(HouseMembershipORM).where(
                            HouseMembershipORM.user_id == user.id,
                            HouseMembershipORM.house_id == floor["house_id"],
                        )
                    )
                    membership.role = "resident"
                else:
                    user.is_active = False
                other.commit()
        return original(repository, houses)

    monkeypatch.setattr(PhysicalDeviceRepository, "lock_houses", boundary)
    if operation == "rename_room":
        response = seeded_client.patch(
            "/rooms/" + room["id"],
            json={
                "name": "not permitted",
                "expected_metadata_revision": room["metadata_revision"],
            },
        )
    elif operation == "create_room":
        response = seeded_client.post(
            "/rooms",
            json={
                "id": "review-child",
                "floor_id": floor["id"],
                "name": "not permitted",
            },
        )
    else:
        response = seeded_client.post(
            "/devices",
            json={
                "id": "review-child",
                "room_id": room["id"],
                "name": "not permitted",
                "type": "relay",
                "capabilities": ["on_off"],
                "state": {"on": False},
            },
        )
    assert hit
    assert (
        response.status_code
        == {"parent_transfer": 409, "revoke": 403, "deactivate": 401}[race]
    ), response.text
    with Session(engine) as db:
        current = db.get(RoomORM, room["id"])
        assert (
            current.name == room["name"]
            and current.metadata_revision == room["metadata_revision"]
        )
        assert db.get(RoomORM, "review-child") is None
        from app.models import DeviceORM, DeviceCommandORM

        assert db.get(DeviceORM, "review-child") is None
        assert db.scalar(select(DeviceCommandORM)) is None
    engine.dispose()
