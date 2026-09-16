from collections.abc import Iterator
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core import Settings
from app.db import Base
from app.main import create_app
from app.models import (
    AutomationORM,
    DeviceORM,
    EventLogORM,
    FloorORM,
    HouseMembershipORM,
    HouseORM,
    RoomORM,
    SceneORM,
)
from app.repositories import MembershipRepository, UserRepository
from app.seed import main as seed
from app.services.auth_service import UserService
from app.events import Event
from app.websocket import ConnectionManager

PASSWORD = "test password for rbac"


@pytest.fixture
def rbac(tmp_path: Path) -> Iterator[tuple[TestClient, Settings, dict[str, str]]]:
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'rbac.db'}",
        app_env="test",
        auth_jwt_secret="rbac-test-secret-that-is-at-least-32-bytes",
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    seed(settings)
    user_ids: dict[str, str] = {}
    setup_engine = create_engine(settings.database_url)
    try:
        with Session(setup_engine) as session:
            users = UserService(UserRepository(session))
            memberships = MembershipRepository(session)
            for role in ("owner", "installer", "technician", "resident"):
                user = users.create(f"{role}@example.test", PASSWORD)
                user_ids[role] = user.id
                memberships.create(
                    {
                        "id": f"membership-{role}",
                        "user_id": user.id,
                        "house_id": "home1",
                        "role": role,
                    }
                )
            foreign = users.create("foreign@example.test", PASSWORD)
            user_ids["foreign"] = foreign.id
            session.add_all(
                [
                    HouseORM(id="hidden-home", name="Hidden"),
                    FloorORM(
                        id="hidden-floor",
                        house_id="hidden-home",
                        name="Hidden",
                        order=1,
                    ),
                    RoomORM(id="hidden-room", floor_id="hidden-floor", name="Hidden"),
                    DeviceORM(
                        id="hidden-device",
                        name="Hidden",
                        room_id="hidden-room",
                        type="light",
                        state={"on": False},
                        capabilities=["on_off"],
                        metadata_={},
                        online=True,
                    ),
                    SceneORM(
                        id="hidden-scene",
                        name="Hidden",
                        house_id="hidden-home",
                        actions=[],
                    ),
                    AutomationORM(
                        id="hidden-automation",
                        name="Hidden",
                        house_id="hidden-home",
                        enabled=True,
                        trigger={
                            "type": "device_state",
                            "device_id": "hidden-device",
                            "field": "on",
                            "operator": "eq",
                            "value": True,
                        },
                        conditions=[],
                        actions=[
                            {
                                "type": "device_state",
                                "device_id": "hidden-device",
                                "state": {"on": False},
                            }
                        ],
                    ),
                    EventLogORM(
                        id="hidden-event",
                        house_id="hidden-home",
                        event_type="device_state_changed",
                        entity_id="hidden-device",
                        payload={},
                        correlation_id="hidden-correlation",
                    ),
                ]
            )
            session.commit()
            memberships.create(
                {
                    "id": "membership-foreign",
                    "user_id": foreign.id,
                    "house_id": "hidden-home",
                    "role": "owner",
                }
            )
    finally:
        setup_engine.dispose()
    with TestClient(create_app(settings, run_simulator=False)) as client:
        yield client, settings, user_ids


def token(client: TestClient, role: str) -> str:
    response = client.post(
        "/auth/login", json={"email": f"{role}@example.test", "password": PASSWORD}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def headers(client: TestClient, role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(client, role)}"}


def test_authentication_house_creation_and_list_isolation(rbac) -> None:
    client, settings, user_ids = rbac
    assert client.get("/houses").status_code == 401
    foreign_headers = headers(client, "foreign")
    created = client.post(
        "/houses",
        json={"id": "foreign-home", "name": "Foreign"},
        headers=foreign_headers,
    )
    assert created.status_code == 201
    assert {
        item["id"] for item in client.get("/houses", headers=foreign_headers).json()
    } == {"hidden-home", "foreign-home"}
    assert (
        client.get(
            "/houses/foreign-home", headers=headers(client, "resident")
        ).status_code
        == 404
    )
    assertion_engine = create_engine(settings.database_url)
    try:
        with Session(assertion_engine) as session:
            membership = session.scalar(
                select(HouseMembershipORM).where(
                    HouseMembershipORM.user_id == user_ids["foreign"],
                    HouseMembershipORM.house_id == "foreign-home",
                )
            )
            assert membership is not None and membership.role == "owner"
            assert session.get(HouseORM, "foreign-home") is not None
    finally:
        assertion_engine.dispose()


def test_role_permissions_and_idor(rbac) -> None:
    client, _, _ = rbac
    resident = headers(client, "resident")
    assert client.get("/devices/living_room_light", headers=resident).status_code == 200
    listed_device = next(
        item
        for item in client.get("/devices", headers=resident).json()
        if item["id"] == "living_room_light"
    )
    assert isinstance(listed_device["metadata"], dict)
    assert (
        client.post("/devices/living_room_light/on", headers=resident).status_code
        == 200
    )
    assert (
        client.delete("/devices/living_room_light", headers=resident).status_code == 403
    )
    assert client.post("/scenes/leave_home/run", headers=resident).status_code == 200
    assert (
        client.patch(
            "/scenes/leave_home", json={"name": "No"}, headers=resident
        ).status_code
        == 403
    )
    assert (
        client.post("/automations/hall_motion_light/run", headers=resident).status_code
        == 403
    )
    assert client.get("/houses/home1/members", headers=resident).status_code == 403

    technician = headers(client, "technician")
    assert (
        client.post("/devices/living_room_light/off", headers=technician).status_code
        == 200
    )
    assert (
        client.patch(
            "/scenes/leave_home", json={"name": "No"}, headers=technician
        ).status_code
        == 403
    )

    installer = headers(client, "installer")
    assert (
        client.patch(
            "/devices/living_room_light", json={"name": "Managed"}, headers=installer
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/automations/hall_motion_light/disable", headers=installer
        ).status_code
        == 200
    )
    assert client.get("/houses/home1/members", headers=installer).status_code == 403

    for path in (
        "/floors/hidden-floor",
        "/rooms/hidden-room",
        "/devices/hidden-device",
        "/scenes/hidden-scene",
        "/automations/hidden-automation",
    ):
        assert client.get(path, headers=resident).status_code == 404
    assert client.post("/devices/hidden-device/on", headers=resident).status_code == 404
    assert client.post("/scenes/hidden-scene/run", headers=resident).status_code == 404
    assert (
        client.post("/automations/hidden-automation/run", headers=installer).status_code
        == 404
    )
    assert (
        client.get(
            "/events", params={"house_id": "hidden-home"}, headers=technician
        ).status_code
        == 404
    )
    assert all(
        item["id"] != "hidden-device"
        for item in client.get("/devices", headers=resident).json()
    )


def test_member_management_and_final_owner_safety(rbac) -> None:
    client, _, user_ids = rbac
    owner = headers(client, "owner")
    added = client.post(
        "/houses/home1/members",
        json={"user_id": user_ids["foreign"], "role": "resident"},
        headers=owner,
    )
    assert added.status_code == 201
    assert (
        client.patch(
            f"/houses/home1/members/{user_ids['foreign']}",
            json={"role": "technician"},
            headers=owner,
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/houses/home1/members/{user_ids['foreign']}", headers=owner
        ).status_code
        == 204
    )
    assert (
        client.patch(
            f"/houses/home1/members/{user_ids['owner']}",
            json={"role": "resident"},
            headers=owner,
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"/houses/home1/members/{user_ids['owner']}", headers=owner
        ).status_code
        == 409
    )


def test_websocket_requires_authentication(rbac) -> None:
    client, _, _ = rbac
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/ws"):
            pass
    assert denied.value.code == 4401


def test_websocket_manager_filters_foreign_house_events() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def accept(self) -> None:
            pass

        async def send_json(self, message: dict) -> None:
            self.messages.append(message)

    async def exercise() -> None:
        manager = ConnectionManager()
        socket = Socket()

        async def authorizes(house_id: str) -> bool:
            return house_id == "home-a"

        await manager.connect(socket, authorizes)  # type: ignore[arg-type]
        await manager.handle_event(
            Event(type="device_online", data={"house_id": "home-b"})
        )
        await manager.handle_event(
            Event(type="device_online", data={"house_id": "home-a"})
        )
        assert len(socket.messages) == 1
        assert socket.messages[0]["house_id"] == "home-a"

    asyncio.run(exercise())
