import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Importing app.main also exposes the production ASGI entry point. Keep that
# import-time application disconnected from every file-backed test database.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["APP_ENV"] = "test"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core import Settings
from app.db import Base
from app.main import create_app
from app.models import DeviceORM, HouseORM
from app.seed import main as seed


@pytest.fixture
def db_settings(tmp_path: Path) -> Iterator[Settings]:
    database_file = tmp_path / "kzhome.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_file.as_posix()}",
        app_env="test",
    )
    engine = create_engine(settings.database_url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    yield settings

    # Every TestClient, Session and Engine created by the test has been closed
    # before this finalizer runs, so cleanup is safe on Windows as well.
    database_file.unlink()


@pytest.fixture
def client(db_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(db_settings, run_simulator=False)) as test_client:
        yield test_client


@pytest.fixture
def seeded_client(db_settings: Settings) -> Iterator[TestClient]:
    seed(db_settings)
    with TestClient(create_app(db_settings, run_simulator=False)) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "version": "0.3.0"}


def test_create_structure_device_and_duplicate_validation(client: TestClient) -> None:
    house = {"id": "test-home", "name": "Test home"}
    assert client.post("/houses", json=house).status_code == 201
    assert client.post("/houses", json=house).status_code == 409
    assert (
        client.post(
            "/floors",
            json={"id": "f1", "house_id": "test-home", "name": "Floor", "order": 1},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/rooms", json={"id": "r1", "floor_id": "f1", "name": "Room"}
        ).status_code
        == 201
    )
    response = client.post(
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
        client.post(
            "/devices",
            json={"id": "bad", "name": "Bad", "room_id": "missing", "type": "light"},
        ).status_code
        == 422
    )


def test_device_state_persists_and_filters(db_settings: Settings) -> None:
    seed(db_settings)
    with TestClient(create_app(db_settings, run_simulator=False)) as first_client:
        response = first_client.patch(
            "/devices/living_room_light/state", json={"on": True, "brightness": 50}
        )
        assert response.json()["state"] == {"on": True, "brightness": 50}

    # The first app has completed its lifespan and disposed its engine. A fresh
    # app still reads the state from the same temporary database.
    with TestClient(create_app(db_settings, run_simulator=False)) as restarted:
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


def test_scene_execution_and_shortcuts(seeded_client: TestClient) -> None:
    seeded_client.post("/devices/living_room_light/on")
    assert seeded_client.post("/scenes/leave_home/run").status_code == 200
    assert (
        seeded_client.get("/devices/living_room_light").json()["state"]["on"] is False
    )
    assert (
        seeded_client.post("/devices/living_room_light/on").json()["state"]["on"]
        is True
    )
    assert (
        seeded_client.post("/devices/living_room_light/off").json()["state"]["on"]
        is False
    )


def test_automation_trigger_and_websocket(seeded_client: TestClient) -> None:
    with seeded_client.websocket_connect("/ws") as websocket:
        assert (
            seeded_client.patch(
                "/devices/hall_motion/state", json={"motion": True}
            ).status_code
            == 200
        )
        assert websocket.receive_json()["device_id"] == "living_room_light"
        assert websocket.receive_json() == {
            "type": "automation_triggered",
            "automation_id": "hall_motion_light",
        }
        assert (
            seeded_client.get("/devices/living_room_light").json()["state"]["on"]
            is True
        )


def test_seed_is_idempotent(db_settings: Settings) -> None:
    seed(db_settings)
    seed(db_settings)
    engine = create_engine(db_settings.database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(HouseORM)) == 1
            assert session.scalar(select(func.count()).select_from(DeviceORM)) == 5
    finally:
        engine.dispose()
