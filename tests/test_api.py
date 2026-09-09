import os
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///./test_kzhome.db"
os.environ["APP_ENV"] = "test"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core import Settings
from app.db import Base
from app.main import create_app
from app.models import DeviceORM, HouseORM
from app.seed import main as seed

TEST_DB = Path("test_kzhome.db")


def make_client(*, seeded: bool = False) -> TestClient:
    if TEST_DB.exists():
        TEST_DB.unlink()
    engine = create_engine(os.environ["DATABASE_URL"])
    Base.metadata.create_all(engine)
    if seeded:
        seed()
    return TestClient(create_app(Settings.from_env(), run_simulator=False))


def test_health() -> None:
    with make_client() as api:
        assert api.get("/health").json() == {"status": "ok", "version": "0.3.0"}


def test_create_structure_device_and_duplicate_validation() -> None:
    with make_client() as api:
        house = {"id": "test-home", "name": "Test home"}
        assert api.post("/houses", json=house).status_code == 201
        assert api.post("/houses", json=house).status_code == 409
        assert (
            api.post(
                "/floors",
                json={"id": "f1", "house_id": "test-home", "name": "Floor", "order": 1},
            ).status_code
            == 201
        )
        assert (
            api.post(
                "/rooms", json={"id": "r1", "floor_id": "f1", "name": "Room"}
            ).status_code
            == 201
        )
        response = api.post(
            "/devices",
            json={
                "id": "light",
                "name": "Light",
                "room_id": "r1",
                "type": "light",
                "state": {"on": False},
                "capabilities": ["on_off"],
                "metadata": {"protocol": "virtual"},
            },
        )
        assert response.status_code == 201
        assert (
            api.post(
                "/devices",
                json={
                    "id": "bad",
                    "name": "Bad",
                    "room_id": "missing",
                    "type": "light",
                },
            ).status_code
            == 422
        )


def test_device_state_persists_and_filters() -> None:
    with make_client(seeded=True) as api:
        response = api.patch(
            "/devices/living_room_light/state", json={"on": True, "brightness": 50}
        )
        assert response.json()["state"] == {"on": True, "brightness": 50}
    with TestClient(create_app(Settings.from_env(), run_simulator=False)) as restarted:
        assert restarted.get("/devices/living_room_light").json()["state"]["on"] is True
        assert (
            len(
                restarted.get(
                    "/devices",
                    params={"room_id": "living_room", "type": "light", "online": True},
                ).json()
            )
            == 1
        )


def test_scene_execution_and_shortcuts() -> None:
    with make_client(seeded=True) as api:
        api.post("/devices/living_room_light/on")
        assert api.post("/scenes/leave_home/run").status_code == 200
        assert api.get("/devices/living_room_light").json()["state"]["on"] is False
        assert api.post("/devices/living_room_light/on").json()["state"]["on"] is True
        assert api.post("/devices/living_room_light/off").json()["state"]["on"] is False


def test_automation_trigger_and_websocket() -> None:
    with make_client(seeded=True) as api, api.websocket_connect("/ws") as websocket:
        assert (
            api.patch("/devices/hall_motion/state", json={"motion": True}).status_code
            == 200
        )
        assert websocket.receive_json()["device_id"] == "living_room_light"
        assert websocket.receive_json() == {
            "type": "automation_triggered",
            "automation_id": "hall_motion_light",
        }
        assert api.get("/devices/living_room_light").json()["state"]["on"] is True


def test_seed_is_idempotent() -> None:
    make_client()
    seed()
    seed()
    with Session(create_engine(os.environ["DATABASE_URL"])) as session:
        assert session.scalar(select(func.count()).select_from(HouseORM)) == 1
        assert session.scalar(select(func.count()).select_from(DeviceORM)) == 5
