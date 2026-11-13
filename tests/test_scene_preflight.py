"""Actual services/SQLite with a recording transport, never hardware or broker."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import Settings
from app.core.errors import InvalidReferenceError
from app.db import Base
from app.events import EventBus
from app.repositories import (
    DeviceRepository,
    RoomRepository,
    HouseRepository,
    SceneRepository,
)
from app.schemas import SceneCreate
from app.seed import main as seed
from app.services.device_service import DeviceService
from app.services.scene_service import SceneService


@pytest.fixture
def scene_stack(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'scenes.db'}", app_env="test"
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    seed(settings)
    try:
        with Session(engine) as session:
            bus = EventBus()
            events = []

            async def record(event):
                events.append(event)

            bus.subscribe(record)
            wire = AsyncMock()
            devices = DeviceService(
                DeviceRepository(session), RoomRepository(session), bus, wire
            )
            scenes = SceneService(
                SceneRepository(session), HouseRepository(session), devices, bus
            )
            yield scenes, devices, wire, events
    finally:
        engine.dispose()


@pytest.mark.parametrize("network", [False, True])
def test_invalid_later_action_never_changes_first_device(scene_stack, network):
    scenes, devices, wire, events = scene_stack
    if not network:
        devices.transport = None
    scenes.create(
        SceneCreate(
            id="invalid",
            name="Invalid",
            house_id="home1",
            actions=[
                {"device_id": "living_room_light", "state": {"on": True}},
                {"device_id": "living_room_curtain", "state": {"on": True}},
            ],
        )
    )
    with pytest.raises(InvalidReferenceError, match="Unsupported state fields"):
        asyncio.run(scenes.run("invalid"))
    wire.send_command.assert_not_called()
    assert devices.get("living_room_light").state["on"] is False
    assert events == []


def test_scene_dispatch_is_not_confirmed_state(scene_stack):
    scenes, devices, wire, events = scene_stack
    asyncio.run(scenes.run("leave_home"))
    assert wire.send_command.await_count == 2
    # The transport has supplied no ACK/state. Returning a definition and a
    # scene_started event cannot attest to the requested curtain position.
    assert devices.get("living_room_curtain").state["position"] == 100
    assert [event.type for event in events] == ["scene_started"]


@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("status", ["active", "inactive"])
def test_later_physical_gate_blocks_entire_scene_admission(
    scene_stack, network, status
):
    from app.models import PhysicalDeviceORM
    from app.core.errors import ConflictError

    scenes, devices, wire, events = scene_stack
    if not network:
        devices.transport = None
    # Inventory rows are synthetic fixtures. No physical gate is patched out.
    session = devices.repository.session
    session.add(
        PhysicalDeviceORM(
            device_id="living_room_curtain",
            bound_device_id="living_room_curtain",
            house_id="home1",
            hardware_id="fixture-only",
            hardware_model="fixture",
            status=status,
            protocol_version="v1",
        )
    )
    session.commit()
    scenes.create(
        SceneCreate(
            id="mixed",
            name="Mixed",
            house_id="home1",
            actions=[
                {"device_id": "living_room_light", "state": {"on": True}},
                {"device_id": "living_room_curtain", "state": {"position": 0}},
            ],
        )
    )
    with pytest.raises(ConflictError, match="Physical"):
        asyncio.run(scenes.run("mixed"))
    wire.send_command.assert_not_called()
    assert devices.get("living_room_light").state["on"] is False
    assert events == []


def test_each_dispatch_rechecks_after_preflight(scene_stack):
    from app.core.errors import InvalidReferenceError

    scenes, devices, wire, events = scene_stack

    async def mutate_after_first_publish(*args, **kwargs):
        if wire.send_command.await_count == 1:
            devices.repository.update("living_room_curtain", {"capabilities": []})

    wire.send_command.side_effect = mutate_after_first_publish
    with pytest.raises(InvalidReferenceError, match="Unsupported"):
        asyncio.run(scenes.run("leave_home"))
    assert wire.send_command.await_count == 1
    assert events == []  # No success event for a partially dispatched scene.
