import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.api import build_router
from app.core import Settings
from app.core.errors import ConflictError, EntityNotFoundError, InvalidReferenceError
from app.db import create_db_engine, create_session_factory, session_dependency
from app.events import Event, EventBus
from app.repositories import (
    AutomationRepository,
    DeviceRepository,
    EventLogRepository,
    HouseRepository,
    RoomRepository,
)
from app.services.automation_service import AutomationService
from app.services.device_service import DeviceService
from app.services.event_log_service import EventLogService
from app.transports import AiomqttClient, MQTTClient, MQTTGateway, Transport
from app.websocket import ConnectionManager
from simulator.virtual_device import VirtualDeviceSimulator, stop_simulator

VERSION = "0.6.0a1"
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    run_simulator: bool | None = None,
    mqtt_client: MQTTClient | None = None,
    mqtt_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = create_db_engine(settings)
    session_factory = create_session_factory(settings, engine)
    get_session = session_dependency(session_factory)
    event_bus = EventBus()
    connections = ConnectionManager()
    automation_tasks: set[asyncio.Task[None]] = set()
    gateway: MQTTGateway | None = None

    if settings.mqtt_enabled:
        client = mqtt_client or AiomqttClient(
            host=settings.mqtt_host or "",
            port=settings.mqtt_port,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            tls_enabled=settings.mqtt_tls_enabled,
            keepalive=settings.mqtt_keepalive,
            client_id=settings.mqtt_client_id or "",
        )

        async def receive_state(
            house_id: str,
            device_id: str,
            state: dict,
            correlation_id: str,
        ) -> None:
            with session_factory() as session:
                await DeviceService(
                    DeviceRepository(session), RoomRepository(session), event_bus
                ).report_state(
                    house_id, device_id, state, correlation_id=correlation_id
                )

        async def receive_status(
            house_id: str, device_id: str, online: bool, last_seen
        ) -> bool:
            with session_factory() as session:
                return await DeviceService(
                    DeviceRepository(session), RoomRepository(session), event_bus
                ).update_status(house_id, device_id, online, last_seen)

        async def validate_identity(house_id: str, device_id: str) -> None:
            with session_factory() as session:
                DeviceService(
                    DeviceRepository(session), RoomRepository(session), event_bus
                ).require_house(house_id, device_id)

        gateway = MQTTGateway(
            client,
            event_bus,
            receive_state,
            receive_status,
            validate_identity,
            sleep=mqtt_sleep,
        )

    command_transport: Transport | None = gateway

    async def process_automations(event: Event) -> None:
        with session_factory() as session:
            devices = DeviceService(
                DeviceRepository(session),
                RoomRepository(session),
                event_bus,
                command_transport,
            )
            service = AutomationService(
                AutomationRepository(session),
                HouseRepository(session),
                devices,
                event_bus,
            )
            await service.handle_event(event)

    async def schedule_automations(event: Event) -> None:
        if event.type != "device_state_changed":
            return
        task = asyncio.create_task(process_automations(event))
        automation_tasks.add(task)

        def task_finished(done: asyncio.Task[None]) -> None:
            automation_tasks.discard(done)
            error = None if done.cancelled() else done.exception()
            if error is not None:
                logger.error(
                    "Automation event processing failed",
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(task_finished)

    async def log_event(event: Event) -> None:
        try:
            with session_factory() as session:
                await EventLogService(EventLogRepository(session)).handle_event(event)
        except Exception:
            logger.exception("Failed to persist event %s", event.type)

    event_bus.subscribe(schedule_automations)
    event_bus.subscribe(log_event)
    event_bus.subscribe(connections.handle_event)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task: asyncio.Task[None] | None = None
        simulator_requested = (
            settings.simulator_enabled if run_simulator is None else run_simulator
        )
        simulator_enabled = (
            settings.app_env.lower() != "production" and simulator_requested
        )
        if simulator_enabled:
            try:
                interval = float(os.getenv("KZHOME_SIMULATOR_INTERVAL", "5"))
            except ValueError:
                logger.exception("Invalid KZHOME_SIMULATOR_INTERVAL; using 5 seconds")
                interval = 5.0
            simulator = VirtualDeviceSimulator(session_factory, event_bus, interval)
            task = asyncio.create_task(
                simulator.run(), name="kzhome-virtual-device-simulator"
            )
        if gateway is not None:
            gateway.start()
        try:
            yield
        finally:
            if gateway is not None:
                await gateway.stop()
            if task is not None:
                await stop_simulator(task)
            for automation_task in tuple(automation_tasks):
                automation_task.cancel()
            if automation_tasks:
                await asyncio.gather(*automation_tasks, return_exceptions=True)
            engine.dispose()

    # API responses never expose tracebacks; APP_DEBUG remains available to
    # infrastructure for local logging configuration.
    application = FastAPI(
        title="KZ Home Core", version=VERSION, debug=False, lifespan=lifespan
    )
    application.state.settings = settings
    application.state.mqtt_gateway = gateway
    application.include_router(
        build_router(get_session, event_bus, command_transport, settings)
    )

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
