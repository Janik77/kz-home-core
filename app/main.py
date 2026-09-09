import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from app.automation import AutomationEngine
from app.models import Device, Room, Scene, SceneAction
from app.services.device_service import DeviceNotFoundError, DeviceService
from app.services.scene_service import SceneNotFoundError, SceneService
from app.websocket import ConnectionManager
from simulator.virtual_device import VirtualMotionSensor, stop_simulator

rooms = [Room(id="living_room", name="Living room")]
devices = DeviceService(
    [
        Device(id="light1", name="Main light", room_id="living_room", type="light", state="off"),
        Device(id="relay1", name="Relay", room_id="living_room", type="relay", state="off"),
        Device(id="motion1", name="Motion sensor", room_id="living_room", type="motion_sensor", state=False),
    ]
)
scenes = SceneService(
    devices,
    [Scene(id="all_off", name="All off", actions=[SceneAction(device_id="light1", state="off"), SceneAction(device_id="relay1", state="off")])],
)
connections = ConnectionManager()
automation = AutomationEngine(devices)
devices.subscribe(automation.handle)
devices.subscribe(connections.broadcast)


@asynccontextmanager
async def lifespan(_: FastAPI):
    interval = float(os.getenv("KZHOME_SIMULATOR_INTERVAL", "5"))
    task = asyncio.create_task(VirtualMotionSensor(devices, interval=interval).run())
    yield
    await stop_simulator(task)


app = FastAPI(title="KZ Home Core", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/devices", response_model=list[Device])
async def list_devices() -> list[Device]:
    return await devices.list()


@app.get("/devices/{device_id}", response_model=Device)
async def get_device(device_id: str) -> Device:
    try:
        return await devices.get(device_id)
    except DeviceNotFoundError as error:
        raise HTTPException(status_code=404, detail="Device not found") from error


async def switch_device(device_id: str, state: str) -> Device:
    try:
        return await devices.set_state(device_id, state)
    except DeviceNotFoundError as error:
        raise HTTPException(status_code=404, detail="Device not found") from error


@app.post("/devices/{device_id}/on", response_model=Device)
async def turn_on(device_id: str) -> Device:
    return await switch_device(device_id, "on")


@app.post("/devices/{device_id}/off", response_model=Device)
async def turn_off(device_id: str) -> Device:
    return await switch_device(device_id, "off")


@app.get("/rooms", response_model=list[Room])
async def list_rooms() -> list[Room]:
    return rooms


@app.get("/scenes", response_model=list[Scene])
async def list_scenes() -> list[Scene]:
    return scenes.list()


@app.post("/scenes/{scene_id}/run", response_model=Scene)
async def run_scene(scene_id: str) -> Scene:
    try:
        return await scenes.run(scene_id)
    except SceneNotFoundError as error:
        raise HTTPException(status_code=404, detail="Scene not found") from error


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await connections.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.disconnect(websocket)
