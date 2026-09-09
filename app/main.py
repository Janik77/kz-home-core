import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from app import demo_data
from app.events import EventBus
from app.models import Device, DeviceState, Floor, House, Room, Scene
from app.services.automation_service import AutomationService
from app.services.device_service import DeviceNotFoundError, DeviceService
from app.services.scene_service import SceneNotFoundError, SceneService
from app.services.structure_service import StructureNotFoundError, StructureService
from app.websocket import ConnectionManager
from simulator.virtual_device import VirtualDeviceSimulator, stop_simulator

VERSION = "0.2.0"


def create_app(*, run_simulator: bool = True) -> FastAPI:
    event_bus = EventBus()
    devices = DeviceService(event_bus, demo_data.devices())
    structures = StructureService(demo_data.houses(), demo_data.floors(), demo_data.rooms())
    scenes = SceneService(devices, event_bus, demo_data.scenes())
    automations = AutomationService(devices, event_bus, demo_data.automations())
    connections = ConnectionManager()
    event_bus.subscribe(automations.handle_event)
    event_bus.subscribe(connections.handle_event)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task: asyncio.Task[None] | None = None
        if run_simulator:
            interval = float(os.getenv("KZHOME_SIMULATOR_INTERVAL", "5"))
            task = asyncio.create_task(VirtualDeviceSimulator(devices, interval).run())
        yield
        if task is not None:
            await stop_simulator(task)

    application = FastAPI(title="KZ Home Core", version=VERSION, lifespan=lifespan)
    application.state.devices = devices
    application.state.automations = automations

    @application.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": VERSION, "devices": len(await devices.list()), "automations": len(automations.list())}

    @application.get("/houses", response_model=list[House])
    async def list_houses() -> list[House]:
        return structures.houses()

    @application.get("/houses/{house_id}", response_model=House)
    async def get_house(house_id: str) -> House:
        return _structure_or_404(structures.house, house_id)

    @application.get("/floors", response_model=list[Floor])
    async def list_floors() -> list[Floor]:
        return structures.floors()

    @application.get("/floors/{floor_id}", response_model=Floor)
    async def get_floor(floor_id: str) -> Floor:
        return _structure_or_404(structures.floor, floor_id)

    @application.get("/rooms", response_model=list[Room])
    async def list_rooms() -> list[Room]:
        return structures.rooms()

    @application.get("/rooms/{room_id}", response_model=Room)
    async def get_room(room_id: str) -> Room:
        return _structure_or_404(structures.room, room_id)

    @application.get("/devices", response_model=list[Device])
    async def list_devices() -> list[Device]:
        return await devices.list()

    @application.get("/devices/{device_id}", response_model=Device)
    async def get_device(device_id: str) -> Device:
        return await _device_or_404(devices, device_id)

    @application.patch("/devices/{device_id}/state", response_model=Device)
    async def patch_device_state(device_id: str, state: DeviceState) -> Device:
        try:
            return await devices.update_state(device_id, state)
        except DeviceNotFoundError as error:
            raise HTTPException(404, "Device not found") from error

    @application.post("/devices/{device_id}/on", response_model=Device)
    async def turn_on(device_id: str) -> Device:
        return await patch_device_state(device_id, {"on": True})

    @application.post("/devices/{device_id}/off", response_model=Device)
    async def turn_off(device_id: str) -> Device:
        return await patch_device_state(device_id, {"on": False})

    @application.get("/scenes", response_model=list[Scene])
    async def list_scenes() -> list[Scene]:
        return scenes.list()

    @application.post("/scenes/{scene_id}/run", response_model=Scene)
    async def run_scene(scene_id: str) -> Scene:
        try:
            return await scenes.run(scene_id)
        except SceneNotFoundError as error:
            raise HTTPException(404, "Scene not found") from error

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await connections.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            connections.disconnect(websocket)

    return application


def _structure_or_404(getter: Any, item_id: str) -> Any:
    try:
        return getter(item_id)
    except StructureNotFoundError as error:
        raise HTTPException(404, "Resource not found") from error


async def _device_or_404(devices: DeviceService, device_id: str) -> Device:
    try:
        return await devices.get(device_id)
    except DeviceNotFoundError as error:
        raise HTTPException(404, "Device not found") from error


app = create_app()
