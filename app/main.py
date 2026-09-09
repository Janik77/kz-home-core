import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.api import build_router
from app.core import Settings
from app.core.errors import ConflictError, EntityNotFoundError, InvalidReferenceError
from app.db import create_session_factory, session_dependency
from app.events import Event, EventBus
from app.repositories import (
    AutomationRepository,
    DeviceRepository,
    HouseRepository,
    RoomRepository,
)
from app.services.automation_service import AutomationService
from app.services.device_service import DeviceService
from app.websocket import ConnectionManager
from simulator.virtual_device import VirtualDeviceSimulator, stop_simulator

VERSION = "0.3.0"


def create_app(
    settings: Settings | None = None, *, run_simulator: bool = True
) -> FastAPI:
    settings = settings or Settings.from_env()
    session_factory = create_session_factory(settings)
    get_session = session_dependency(session_factory)
    event_bus = EventBus()
    connections = ConnectionManager()

    async def run_automations(event: Event) -> None:
        with session_factory() as session:
            devices = DeviceService(
                DeviceRepository(session), RoomRepository(session), event_bus
            )
            service = AutomationService(
                AutomationRepository(session),
                HouseRepository(session),
                devices,
                event_bus,
            )
            await service.handle_event(event)

    event_bus.subscribe(run_automations)
    event_bus.subscribe(connections.handle_event)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task: asyncio.Task[None] | None = None
        simulator_session = None
        if run_simulator:
            simulator_session = session_factory()
            devices = DeviceService(
                DeviceRepository(simulator_session),
                RoomRepository(simulator_session),
                event_bus,
            )
            interval = float(os.getenv("KZHOME_SIMULATOR_INTERVAL", "5"))
            task = asyncio.create_task(VirtualDeviceSimulator(devices, interval).run())
        yield
        if task is not None:
            await stop_simulator(task)
        if simulator_session is not None:
            simulator_session.close()

    # API responses never expose tracebacks; APP_DEBUG remains available to
    # infrastructure for local logging configuration.
    application = FastAPI(
        title="KZ Home Core", version=VERSION, debug=False, lifespan=lifespan
    )
    application.state.settings = settings
    application.include_router(build_router(get_session, event_bus))

    @application.exception_handler(EntityNotFoundError)
    async def not_found_handler(_, error: EntityNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"detail": str(error) or "Entity not found"}
        )

    @application.exception_handler(ConflictError)
    async def conflict_handler(_, error: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @application.exception_handler(InvalidReferenceError)
    async def invalid_reference_handler(
        _, error: InvalidReferenceError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await connections.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            connections.disconnect(websocket)

    return application


app = create_app()
