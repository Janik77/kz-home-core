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
from app.repositories import MembershipRepository, UserRepository
from app.services.auth_service import UserService
from app.seed import main as seed
from app.transports import FakeMQTTClient

OWNER_EMAIL = "owner@example.test"
OWNER_PASSWORD = "test owner password"


def create_test_owner(settings: Settings, house_id: str | None = None) -> None:
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            user = UserService(UserRepository(session)).create(
                OWNER_EMAIL, OWNER_PASSWORD
            )
            if house_id is not None:
                MembershipRepository(session).create(
                    {
                        "id": "test-owner",
                        "user_id": user.id,
                        "house_id": house_id,
                        "role": "owner",
                    }
                )
    finally:
        engine.dispose()


def authenticate_test_owner(client: TestClient) -> None:
    login = client.post(
        "/auth/login",
        json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
    )
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"


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
    create_test_owner(db_settings)
    with TestClient(create_app(db_settings, run_simulator=False)) as test_client:
        authenticate_test_owner(test_client)
        yield test_client


@pytest.fixture
def seeded_client(db_settings: Settings) -> Iterator[TestClient]:
    seed(db_settings)
    create_test_owner(db_settings, "home1")
    with TestClient(create_app(db_settings, run_simulator=False)) as test_client:
        authenticate_test_owner(test_client)
        test_client.auth_access_token = test_client.headers["Authorization"].split()[1]  # type: ignore[attr-defined]
        yield test_client


def test_lifespan_starts_and_stops_when_simulator_disabled(
    client: TestClient,
) -> None:
    assert client.get("/health").json() == {"status": "ok", "version": "0.6.0b1"}


def test_enabled_simulator_does_not_block_startup(
    db_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KZHOME_SIMULATOR_INTERVAL", "60")
    enabled_settings = Settings(
        database_url=db_settings.database_url,
        app_env="development",
        simulator_enabled=True,
    )
    with TestClient(create_app(enabled_settings)) as test_client:
        assert test_client.get("/health").status_code == 200


def test_mqtt_disabled_does_not_connect(db_settings: Settings) -> None:
    mqtt = FakeMQTTClient()
    with TestClient(
        create_app(db_settings, run_simulator=False, mqtt_client=mqtt)
    ) as test_client:
        assert test_client.get("/health").json()["version"] == "0.6.0b1"
    assert mqtt.connect_calls == 0


def test_broker_failure_does_not_block_api_startup(db_settings: Settings) -> None:
    mqtt = FakeMQTTClient(connect_error=ConnectionError("broker unavailable"))
    enabled = Settings(
        database_url=db_settings.database_url,
        app_env="test",
        mqtt_enabled=True,
        mqtt_host="broker.invalid",
        mqtt_client_id="test-core",
    )
    with TestClient(
        create_app(enabled, run_simulator=False, mqtt_client=mqtt)
    ) as test_client:
        assert test_client.get("/health").status_code == 200


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
    assert response.json()["metadata"] == {"protocol": "virtual"}
    assert (
        client.post(
            "/devices",
            json={"id": "bad", "name": "Bad", "room_id": "missing", "type": "light"},
        ).status_code
        # Missing and foreign parent IDs share the non-disclosing IDOR response.
        == 404
    )


def test_device_state_persists_and_filters(db_settings: Settings) -> None:
    seed(db_settings)
    create_test_owner(db_settings, "home1")
    with TestClient(create_app(db_settings, run_simulator=False)) as first_client:
        authenticate_test_owner(first_client)
        response = first_client.patch(
            "/devices/living_room_light/state", json={"on": True, "brightness": 50}
        )
        assert response.status_code == 200
        assert response.json()["state"] == {"on": True, "brightness": 50}

    # The first app has completed its lifespan and disposed its engine. A fresh
    # app still reads the state from the same temporary database.
    with TestClient(create_app(db_settings, run_simulator=False)) as restarted:
        authenticate_test_owner(restarted)
        persisted = restarted.get("/devices/living_room_light")
        assert persisted.status_code == 200
        assert persisted.json()["state"]["on"] is True
        filtered = restarted.get(
            "/devices",
            params={"room_id": "living_room", "type": "light", "online": True},
        )
        assert filtered.status_code == 200
        assert len(filtered.json()) == 1


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


def test_demo_state_event_does_not_execute_legacy_automation(
    seeded_client: TestClient,
) -> None:
    # HTTP/demo mutations are not fresh authenticated MQTT reports.
    with seeded_client.websocket_connect(
        "/ws", headers={"Authorization": f"Bearer {seeded_client.auth_access_token}"}
    ) as websocket:
        assert (
            seeded_client.patch(
                "/devices/hall_motion/state", json={"motion": True}
            ).status_code
            == 200
        )
        event = websocket.receive_json()
        assert event["type"] == "device_state_changed"
        assert event["device_id"] == "hall_motion"
    assert (
        seeded_client.get("/devices/living_room_light").json()["state"]["on"] is False
    )
    assert seeded_client.get("/automations/hall_motion_light/runs").json() == []


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


def automation_payload(
    automation_id: str,
    *,
    trigger: dict | None = None,
    conditions: list[dict] | None = None,
    actions: list[dict] | None = None,
    enabled: bool = True,
) -> dict:
    return {
        "id": automation_id,
        "house_id": "home1",
        "name": automation_id,
        "enabled": enabled,
        "trigger": trigger
        or {
            "type": "device_state",
            "device_id": "hall_motion",
            "field": "motion",
            "operator": "eq",
            "value": True,
        },
        "conditions": conditions or [],
        "actions": actions
        or [
            {
                "type": "device_state",
                "device_id": "living_room_light",
                "state": {"on": True},
            }
        ],
    }


def test_invalid_rule_and_capability_validation(seeded_client: TestClient) -> None:
    invalid = automation_payload("invalid")
    invalid["trigger"]["operator"] = "contains"
    assert seeded_client.post("/automations", json=invalid).status_code == 422

    unsupported = automation_payload(
        "unsupported",
        actions=[
            {
                "type": "device_state",
                "device_id": "hall_motion",
                "state": {"brightness": 50},
            }
        ],
    )
    assert seeded_client.post("/automations", json=unsupported).status_code == 422


def test_disabled_automation_and_manual_run(seeded_client: TestClient) -> None:
    seeded_client.post("/automations/hall_motion_light/disable")
    payload = automation_payload("manual", enabled=False)
    assert seeded_client.post("/automations", json=payload).status_code == 201
    seeded_client.patch("/devices/hall_motion/state", json={"motion": True})
    assert (
        seeded_client.get("/devices/living_room_light").json()["state"]["on"] is False
    )

    assert seeded_client.post("/automations/manual/run").status_code == 409
    assert (
        seeded_client.get("/devices/living_room_light").json()["state"]["on"] is False
    )
    assert seeded_client.post("/automations/manual/enable").json()["enabled"] is True
    assert seeded_client.post("/automations/manual/disable").json()["enabled"] is False


def test_unsupported_delay_is_preserved_but_fails_closed(
    seeded_client: TestClient,
) -> None:
    payload = automation_payload(
        "delayed_off",
        actions=[
            {"type": "delay", "seconds": 0.01},
            {
                "type": "device_state",
                "device_id": "living_room_light",
                "state": {"on": False},
            },
        ],
    )
    assert seeded_client.post("/automations", json=payload).status_code == 201
    # Preserve legacy definitions for editing, but never execute unsupported delay actions.
    dry = seeded_client.post("/automations/delayed_off/dry-run")
    assert dry.status_code == 200
    assert not dry.json()["eligible"]
    assert "unsupported_definition" in dry.json()["issues"]
    assert seeded_client.post("/automations/delayed_off/run").status_code == 409
    assert (
        seeded_client.get("/devices/living_room_light").json()["state"]["on"] is False
    )


def test_comparison_and_time_conditions() -> None:
    from datetime import time

    from app.schemas import TimeCondition
    from app.services.automation_service import compare, time_matches

    assert compare(25, "gt", 20)
    assert compare(15, "lt", 20)
    daytime = TimeCondition(type="time", after="08:00", before="18:00")
    overnight = TimeCondition(type="time", after="23:00", before="07:00")
    assert time_matches(daytime, time(12, 0))
    assert not time_matches(daytime, time(22, 0))
    assert time_matches(overnight, time(23, 30))
    assert time_matches(overnight, time(6, 30))
    assert not time_matches(overnight, time(12, 0))


def test_multiple_conditions_are_and_and_cross_house_is_rejected(
    seeded_client: TestClient,
) -> None:
    seeded_client.post("/automations/hall_motion_light/disable")
    payload = automation_payload(
        "and_rule",
        conditions=[
            {
                "type": "device_state",
                "device_id": "bedroom_temperature",
                "field": "temperature",
                "operator": "gt",
                "value": 20,
            },
            {
                "type": "device_state",
                "device_id": "living_room_curtain",
                "field": "position",
                "operator": "lt",
                "value": 50,
            },
        ],
    )
    assert seeded_client.post("/automations", json=payload).status_code == 201
    seeded_client.patch("/devices/hall_motion/state", json={"motion": True})
    assert (
        seeded_client.get("/devices/living_room_light").json()["state"]["on"] is False
    )
    seeded_client.patch("/devices/hall_motion/state", json={"motion": False})
    seeded_client.patch("/devices/living_room_curtain/state", json={"position": 30})
    seeded_client.patch("/devices/hall_motion/state", json={"motion": True})
    dry = seeded_client.post("/automations/and_rule/dry-run").json()
    assert not dry["eligible"]
    assert "unsupported_definition" in dry["issues"]
    assert (
        seeded_client.get("/devices/living_room_light").json()["state"]["on"] is False
    )

    seeded_client.post("/houses", json={"id": "other", "name": "Other"})
    seeded_client.post(
        "/floors",
        json={"id": "other_floor", "house_id": "other", "name": "F", "order": 1},
    )
    seeded_client.post(
        "/rooms", json={"id": "other_room", "floor_id": "other_floor", "name": "R"}
    )
    seeded_client.post(
        "/devices",
        json={
            "id": "other_light",
            "name": "Other light",
            "room_id": "other_room",
            "type": "light",
            "state": {"on": False},
            "capabilities": ["on_off"],
        },
    )
    cross_house = automation_payload(
        "cross_house",
        actions=[
            {
                "type": "device_state",
                "device_id": "other_light",
                "state": {"on": True},
            }
        ],
    )
    assert seeded_client.post("/automations", json=cross_house).status_code == 422


def test_automation_failure_does_not_stop_other_rules(
    seeded_client: TestClient,
) -> None:
    seeded_client.post("/automations/hall_motion_light/disable")
    seeded_client.post(
        "/devices",
        json={
            "id": "temporary_light",
            "name": "Temporary",
            "room_id": "hall",
            "type": "light",
            "state": {"on": False},
            "capabilities": ["on_off"],
        },
    )
    assert (
        seeded_client.post(
            "/automations",
            json=automation_payload(
                "will_fail",
                actions=[
                    {
                        "type": "device_state",
                        "device_id": "temporary_light",
                        "state": {"on": True},
                    }
                ],
            ),
        ).status_code
        == 201
    )
    assert (
        seeded_client.post(
            "/automations", json=automation_payload("still_runs")
        ).status_code
        == 201
    )
    seeded_client.delete("/devices/temporary_light")

    for entity_id in ("will_fail", "still_runs"):
        result = seeded_client.post(f"/automations/{entity_id}/dry-run")
        assert result.status_code == 200
        assert result.json()["eligible"] is False
        assert seeded_client.get(f"/automations/{entity_id}/runs").json() == []


def test_loop_protection_ignores_events_at_max_depth(db_settings: Settings) -> None:
    import asyncio

    from app.events import Event, EventBus
    from app.repositories import (
        AutomationRepository,
        DeviceRepository,
        HouseRepository,
        RoomRepository,
    )
    from app.services.automation_service import AutomationService, MAX_AUTOMATION_DEPTH
    from app.services.device_service import DeviceService

    seed(db_settings)
    engine = create_engine(db_settings.database_url)
    try:
        with Session(engine) as session:
            bus = EventBus()
            devices = DeviceService(
                DeviceRepository(session), RoomRepository(session), bus
            )
            service = AutomationService(
                AutomationRepository(session),
                HouseRepository(session),
                devices,
                bus,
            )
            event = Event(
                type="device_state_changed",
                data={
                    "device_id": "hall_motion",
                    "house_id": "home1",
                    "state": {"motion": True},
                },
                depth=MAX_AUTOMATION_DEPTH,
            )
            asyncio.run(service.handle_event(event))
            assert devices.get("living_room_light").state["on"] is False
    finally:
        engine.dispose()
