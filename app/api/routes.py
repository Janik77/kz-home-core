from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.events import EventBus
from app.repositories import (
    AutomationRepository,
    DeviceRepository,
    EventLogRepository,
    FloorRepository,
    HouseRepository,
    RoomRepository,
    SceneRepository,
)
from app.schemas import (
    AutomationCreate,
    AutomationRead,
    AutomationUpdate,
    DeviceCreate,
    DeviceRead,
    DeviceState,
    DeviceType,
    DeviceUpdate,
    FloorCreate,
    FloorRead,
    FloorUpdate,
    HouseCreate,
    HouseRead,
    HouseUpdate,
    RoomCreate,
    RoomRead,
    RoomUpdate,
    SceneCreate,
    SceneRead,
    SceneUpdate,
    EventLogRead,
)
from app.services.automation_service import AutomationService
from app.services.crud_service import CrudService
from app.services.device_service import DeviceService
from app.services.event_log_service import EventLogService
from app.services.scene_service import SceneService


def build_router(get_session: Any, event_bus: EventBus) -> APIRouter:
    router = APIRouter()

    def structure(session: Session, kind: str) -> CrudService[Any]:
        houses = HouseRepository(session)
        if kind == "house":
            return CrudService(houses, HouseRead)
        floors = FloorRepository(session)
        if kind == "floor":
            return CrudService(floors, FloorRead, {"house_id": houses})
        return CrudService(RoomRepository(session), RoomRead, {"floor_id": floors})

    def device_service(session: Session) -> DeviceService:
        return DeviceService(
            DeviceRepository(session), RoomRepository(session), event_bus
        )

    def scene_service(session: Session) -> SceneService:
        return SceneService(
            SceneRepository(session),
            HouseRepository(session),
            device_service(session),
            event_bus,
        )

    def automation_service(session: Session) -> AutomationService:
        return AutomationService(
            AutomationRepository(session),
            HouseRepository(session),
            device_service(session),
            event_bus,
        )

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.4.0"}

    @router.post("/houses", response_model=HouseRead, status_code=201)
    def create_house(data: HouseCreate, session: Session = Depends(get_session)):
        return structure(session, "house").create(data)

    @router.get("/houses", response_model=list[HouseRead])
    def list_houses(session: Session = Depends(get_session)):
        return structure(session, "house").list()

    @router.get("/houses/{entity_id}", response_model=HouseRead)
    def get_house(entity_id: str, session: Session = Depends(get_session)):
        return structure(session, "house").get(entity_id)

    @router.patch("/houses/{entity_id}", response_model=HouseRead)
    def update_house(
        entity_id: str, data: HouseUpdate, session: Session = Depends(get_session)
    ):
        return structure(session, "house").update(entity_id, data)

    @router.delete("/houses/{entity_id}", status_code=204)
    def delete_house(entity_id: str, session: Session = Depends(get_session)):
        structure(session, "house").delete(entity_id)
        return Response(status_code=204)

    @router.post("/floors", response_model=FloorRead, status_code=201)
    def create_floor(data: FloorCreate, session: Session = Depends(get_session)):
        return structure(session, "floor").create(data)

    @router.get("/floors", response_model=list[FloorRead])
    def list_floors(session: Session = Depends(get_session)):
        return structure(session, "floor").list()

    @router.get("/floors/{entity_id}", response_model=FloorRead)
    def get_floor(entity_id: str, session: Session = Depends(get_session)):
        return structure(session, "floor").get(entity_id)

    @router.patch("/floors/{entity_id}", response_model=FloorRead)
    def update_floor(
        entity_id: str, data: FloorUpdate, session: Session = Depends(get_session)
    ):
        return structure(session, "floor").update(entity_id, data)

    @router.delete("/floors/{entity_id}", status_code=204)
    def delete_floor(entity_id: str, session: Session = Depends(get_session)):
        structure(session, "floor").delete(entity_id)
        return Response(status_code=204)

    @router.post("/rooms", response_model=RoomRead, status_code=201)
    def create_room(data: RoomCreate, session: Session = Depends(get_session)):
        return structure(session, "room").create(data)

    @router.get("/rooms", response_model=list[RoomRead])
    def list_rooms(session: Session = Depends(get_session)):
        return structure(session, "room").list()

    @router.get("/rooms/{entity_id}", response_model=RoomRead)
    def get_room(entity_id: str, session: Session = Depends(get_session)):
        return structure(session, "room").get(entity_id)

    @router.patch("/rooms/{entity_id}", response_model=RoomRead)
    def update_room(
        entity_id: str, data: RoomUpdate, session: Session = Depends(get_session)
    ):
        return structure(session, "room").update(entity_id, data)

    @router.delete("/rooms/{entity_id}", status_code=204)
    def delete_room(entity_id: str, session: Session = Depends(get_session)):
        structure(session, "room").delete(entity_id)
        return Response(status_code=204)

    @router.post("/devices", response_model=DeviceRead, status_code=201)
    def create_device(data: DeviceCreate, session: Session = Depends(get_session)):
        return device_service(session).create(data)

    @router.get("/devices", response_model=list[DeviceRead])
    def list_devices(
        house_id: str | None = None,
        room_id: str | None = None,
        type: DeviceType | None = Query(None),
        online: bool | None = None,
        session: Session = Depends(get_session),
    ):
        return device_service(session).list(house_id, room_id, type, online)

    @router.get("/devices/{entity_id}", response_model=DeviceRead)
    def get_device(entity_id: str, session: Session = Depends(get_session)):
        return device_service(session).get(entity_id)

    @router.patch("/devices/{entity_id}", response_model=DeviceRead)
    def update_device(
        entity_id: str, data: DeviceUpdate, session: Session = Depends(get_session)
    ):
        return device_service(session).update(entity_id, data)

    @router.delete("/devices/{entity_id}", status_code=204)
    def delete_device(entity_id: str, session: Session = Depends(get_session)):
        device_service(session).delete(entity_id)
        return Response(status_code=204)

    @router.patch("/devices/{entity_id}/state", response_model=DeviceRead)
    async def update_state(
        entity_id: str, state: DeviceState, session: Session = Depends(get_session)
    ):
        return await device_service(session).update_state(entity_id, state)

    @router.post("/devices/{entity_id}/on", response_model=DeviceRead)
    async def turn_on(entity_id: str, session: Session = Depends(get_session)):
        return await device_service(session).update_state(entity_id, {"on": True})

    @router.post("/devices/{entity_id}/off", response_model=DeviceRead)
    async def turn_off(entity_id: str, session: Session = Depends(get_session)):
        return await device_service(session).update_state(entity_id, {"on": False})

    @router.post("/scenes", response_model=SceneRead, status_code=201)
    def create_scene(data: SceneCreate, session: Session = Depends(get_session)):
        return scene_service(session).create(data)

    @router.get("/scenes", response_model=list[SceneRead])
    def list_scenes(session: Session = Depends(get_session)):
        return scene_service(session).list()

    @router.get("/scenes/{entity_id}", response_model=SceneRead)
    def get_scene(entity_id: str, session: Session = Depends(get_session)):
        return scene_service(session).get(entity_id)

    @router.patch("/scenes/{entity_id}", response_model=SceneRead)
    def update_scene(
        entity_id: str, data: SceneUpdate, session: Session = Depends(get_session)
    ):
        return scene_service(session).update(entity_id, data)

    @router.delete("/scenes/{entity_id}", status_code=204)
    def delete_scene(entity_id: str, session: Session = Depends(get_session)):
        scene_service(session).delete(entity_id)
        return Response(status_code=204)

    @router.post("/scenes/{entity_id}/run", response_model=SceneRead)
    async def run_scene(entity_id: str, session: Session = Depends(get_session)):
        return await scene_service(session).run(entity_id)

    @router.post("/automations", response_model=AutomationRead, status_code=201)
    def create_automation(
        data: AutomationCreate, session: Session = Depends(get_session)
    ):
        return automation_service(session).create(data)

    @router.get("/automations", response_model=list[AutomationRead])
    def list_automations(session: Session = Depends(get_session)):
        return automation_service(session).list()

    @router.get("/automations/{entity_id}", response_model=AutomationRead)
    def get_automation(entity_id: str, session: Session = Depends(get_session)):
        return automation_service(session).get(entity_id)

    @router.patch("/automations/{entity_id}", response_model=AutomationRead)
    def update_automation(
        entity_id: str, data: AutomationUpdate, session: Session = Depends(get_session)
    ):
        return automation_service(session).update(entity_id, data)

    @router.delete("/automations/{entity_id}", status_code=204)
    def delete_automation(entity_id: str, session: Session = Depends(get_session)):
        automation_service(session).delete(entity_id)
        return Response(status_code=204)

    @router.post("/automations/{entity_id}/enable", response_model=AutomationRead)
    def enable_automation(entity_id: str, session: Session = Depends(get_session)):
        return automation_service(session).set_enabled(entity_id, True)

    @router.post("/automations/{entity_id}/disable", response_model=AutomationRead)
    def disable_automation(entity_id: str, session: Session = Depends(get_session)):
        return automation_service(session).set_enabled(entity_id, False)

    @router.post("/automations/{entity_id}/run", response_model=AutomationRead)
    async def run_automation(entity_id: str, session: Session = Depends(get_session)):
        return await automation_service(session).manual_run(entity_id)

    @router.get("/events", response_model=list[EventLogRead])
    def list_events(
        house_id: str | None = None,
        event_type: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        session: Session = Depends(get_session),
    ):
        return EventLogService(EventLogRepository(session)).list(
            house_id, event_type, limit
        )

    return router
