"""Actual auth/API isolation; no device commands, SQLite as a software test store."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.core import Settings
from app.db import Base
from app.models import (
    DeviceFavoriteORM,
    DeviceORM,
    FloorORM,
    RoomORM,
    HouseORM,
    HouseMembershipORM,
    PhysicalDeviceORM,
    UserORM,
    DeviceCommandORM,
)
from app.repositories import UserRepository, MembershipRepository
from app.services.auth_service import UserService
from test_api import create_app  # Import-time ASGI uses test_api's isolated memory URL.

BASE = "/v1/houses/a/me/device-favorites"


@pytest.fixture
def favorites(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'favorites.db'}", app_env="test"
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    users = {}
    with Session(engine) as session:
        session.add_all([HouseORM(id=h, name=h) for h in ("a", "b")])
        session.flush()
        for h in ("a", "b"):
            session.add(FloorORM(id="f" + h, house_id=h, name=h, order=0))
            session.flush()
            session.add(RoomORM(id="r" + h, floor_id="f" + h, name=h))
            session.flush()
            session.add(
                DeviceORM(
                    id="d" + h,
                    room_id="r" + h,
                    name=h,
                    type="relay",
                    online=True,
                    capabilities=["on_off"],
                    state={"on": False},
                    metadata_={},
                )
            )
        session.commit()
        for name, houses in (("one", ["a"]), ("two", ["a", "b"]), ("foreign", ["b"])):
            users[name] = (
                UserService(UserRepository(session))
                .create(name + "@example.test", "favorite-test-password")
                .id
            )
            for house in houses:
                MembershipRepository(session).create(
                    {
                        "id": name + house,
                        "user_id": users[name],
                        "house_id": house,
                        "role": "resident",
                    }
                )
    try:
        with TestClient(create_app(settings, run_simulator=False)) as client:
            headers = {}
            for name in users:
                login = client.post(
                    "/auth/login",
                    json={
                        "email": name + "@example.test",
                        "password": "favorite-test-password",
                    },
                )
                assert login.status_code == 200
                headers[name] = {
                    "Authorization": "Bearer " + login.json()["access_token"]
                }
            yield client, engine, users, headers, settings
    finally:
        engine.dispose()


def test_personal_idempotency_auth_and_restart(favorites):
    client, engine, users, headers, settings = favorites
    assert client.get(BASE).status_code == 401
    assert client.get(BASE, headers=headers["foreign"]).status_code == 404
    for _ in range(2):
        assert client.put(BASE + "/da", headers=headers["one"]).status_code == 204
    assert client.get(BASE, headers=headers["one"]).json() == {"device_ids": ["da"]}
    assert client.get(BASE, headers=headers["two"]).json() == {"device_ids": []}
    assert (
        client.get(BASE, headers=headers["one"]).headers["cache-control"] == "no-store"
    )
    assert client.put(BASE + "/db", headers=headers["one"]).status_code == 404
    assert client.put(BASE + "/missing", headers=headers["one"]).status_code == 404
    assert (
        client.put(
            BASE + "/da", headers=headers["one"], json={"user_id": users["two"]}
        ).status_code
        == 422
    )
    # A supplied query user_id cannot switch the authenticated principal.
    assert client.get(
        BASE + "?user_id=" + users["one"], headers=headers["two"]
    ).json() == {"device_ids": []}
    with TestClient(create_app(settings, run_simulator=False)) as restarted:
        assert restarted.get(BASE, headers=headers["one"]).json() == {
            "device_ids": ["da"]
        }
    with Session(engine) as session:
        assert session.query(DeviceFavoriteORM).count() == 1
        assert session.get(DeviceORM, "da").state == {"on": False}
        assert session.query(DeviceCommandORM).count() == 0
    # DELETE only touches caller scope; unknown IDs do not leak existence.
    assert client.delete(BASE + "/da", headers=headers["two"]).status_code == 204
    assert client.get(BASE, headers=headers["one"]).json()["device_ids"] == ["da"]
    for _ in range(2):
        assert client.delete(BASE + "/da", headers=headers["one"]).status_code == 204
    assert client.delete(BASE + "/db", headers=headers["one"]).status_code == 204
    assert client.get(BASE, headers=headers["one"]).json()["device_ids"] == []


@pytest.mark.parametrize("move", ["device", "room", "floor"])
def test_location_changes_never_transfer_personal_favorite(favorites, move):
    client, engine, _, headers, _ = favorites
    assert client.put(BASE + "/da", headers=headers["two"]).status_code == 204
    with Session(engine) as session:
        if move == "device":
            session.get(DeviceORM, "da").room_id = "rb"
        elif move == "room":
            session.get(RoomORM, "ra").floor_id = "fb"
        else:
            session.get(FloorORM, "fa").house_id = "b"
        session.commit()
    assert client.get(BASE, headers=headers["two"]).json()["device_ids"] == []
    other = BASE.replace("/a/", "/b/")
    assert client.get(other, headers=headers["two"]).json()["device_ids"] == []
    assert client.put(BASE + "/da", headers=headers["two"]).status_code == 404
    assert client.put(other + "/da", headers=headers["two"]).status_code == 204
    assert client.delete(BASE + "/da", headers=headers["two"]).status_code == 204
    assert client.get(other, headers=headers["two"]).json()["device_ids"] == ["da"]


def test_membership_removal_and_device_deletion_clear_preferences(favorites):
    client, engine, users, headers, _ = favorites
    for name in ("one", "two"):
        assert client.put(BASE + "/da", headers=headers[name]).status_code == 204
    with Session(engine) as session:
        membership = session.scalar(
            select(HouseMembershipORM).filter_by(user_id=users["one"], house_id="a")
        )
        session.delete(membership)
        session.commit()
        assert session.query(DeviceFavoriteORM).count() == 1
        MembershipRepository(session).create(
            {
                "id": "rejoined",
                "user_id": users["one"],
                "house_id": "a",
                "role": "resident",
            }
        )
    assert client.get(BASE, headers=headers["one"]).json()["device_ids"] == []
    with Session(engine) as session:
        session.delete(session.get(DeviceORM, "da"))
        session.commit()
        assert session.query(DeviceFavoriteORM).count() == 0
    assert client.get(BASE, headers=headers["two"]).json()["device_ids"] == []


def test_inactive_user_and_removed_membership_cannot_read_or_write(favorites):
    client, engine, users, headers, _ = favorites
    with Session(engine) as session:
        session.get(UserORM, users["one"]).is_active = False
        session.delete(session.get(HouseMembershipORM, "twoa"))
        session.commit()
    for method in (client.get, client.put, client.delete):
        path = BASE if method == client.get else BASE + "/da"
        assert method(path, headers=headers["one"]).status_code == 401
        assert method(path, headers=headers["two"]).status_code == 404


def test_physical_binding_remains_authoritative_for_read_scope(favorites):
    client, engine, _, headers, _ = favorites
    with Session(engine) as session:
        session.add(
            PhysicalDeviceORM(
                device_id="da",
                bound_device_id="da",
                house_id="a",
                hardware_id="synthetic-only",
                hardware_model="fixture",
                status="inactive",
                protocol_version="v1",
            )
        )
        session.get(
            DeviceORM, "da"
        ).room_id = "rb"  # Synthetic corrupted structure, no hardware.
        session.commit()
    assert client.put(BASE + "/da", headers=headers["one"]).status_code == 204
    assert client.get(BASE, headers=headers["one"]).json()["device_ids"] == ["da"]
    assert (
        client.put(
            BASE.replace("/a/", "/b/") + "/da", headers=headers["two"]
        ).status_code
        == 404
    )


def test_logout_and_account_switch_do_not_change_saved_preferences(favorites):
    client, _, _, headers, _ = favorites
    assert client.put(BASE + "/da", headers=headers["one"]).status_code == 204
    login = client.post(
        "/auth/login",
        json={"email": "one@example.test", "password": "favorite-test-password"},
    ).json()
    assert (
        client.post(
            "/auth/logout", json={"refresh_token": login["refresh_token"]}
        ).status_code
        == 204
    )
    assert client.get(BASE).status_code == 401  # Web discards bearer on logout.
    assert client.get(BASE, headers=headers["two"]).json() == {"device_ids": []}
    # Current Core's access JWT remains valid until expiry; no auth change here.
    assert client.get(
        BASE, headers={"Authorization": "Bearer " + login["access_token"]}
    ).json() == {"device_ids": ["da"]}
    again = client.post(
        "/auth/login",
        json={"email": "one@example.test", "password": "favorite-test-password"},
    ).json()
    assert client.get(
        BASE, headers={"Authorization": "Bearer " + again["access_token"]}
    ).json() == {"device_ids": ["da"]}
