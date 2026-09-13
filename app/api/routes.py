from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.events import EventBus
from app.core import Settings
from app.events import Event
from app.repositories import (
    AutomationRepository,
    DeviceRepository,
    EventLogRepository,
    FloorRepository,
    HouseRepository,
    RoomRepository,
    SceneRepository,
    RefreshSessionRepository,
    UserRepository,
    MembershipRepository,
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
    LoginRequest,
    TokenRequest,
    TokenResponse,
    UserRead,
    MembershipCreate,
    MembershipRead,
    MembershipUpdate,
)
from app.security import Permission, TokenCodec
from app.api.dependencies import build_current_user_dependency
from app.services.auth_service import (
    AuthenticationError,
    AuthenticationService,
    AuthorizationService,
    MembershipService,
)
from app.models import UserORM
from app.services.automation_service import AutomationService
from app.services.crud_service import CrudService
from app.services.device_service import DeviceService
from app.services.event_log_service import EventLogService
from app.services.scene_service import SceneService
from app.transports import Transport


def build_router(
    get_session: Any,
    event_bus: EventBus,
    transport: Transport | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    router = APIRouter()
    if settings is None:
        raise ValueError("Settings are required for authentication")

    codec = TokenCodec(
        settings.auth_jwt_secret,
        settings.auth_jwt_algorithm,
        settings.auth_access_token_minutes,
        settings.auth_refresh_token_days,
    )

    def auth_service(session: Session) -> AuthenticationService:
        return AuthenticationService(
            UserRepository(session), RefreshSessionRepository(session), codec
        )

    def authorization(session: Session) -> AuthorizationService:
        return AuthorizationService(MembershipRepository(session))

    def require(
        session: Session, user: UserORM, house_id: str, permission: Permission
    ) -> None:
        service = authorization(session)
        membership = service.get_house_membership(user.id, house_id)
        if membership is None:
            # Deliberately hide whether an object in another house exists.
            raise HTTPException(404, "Resource not found")
        if not service.has_permission(membership, permission):
            EventLogService(EventLogRepository(session)).authorization_denied(
                user.id, house_id, permission.value
            )
            raise HTTPException(403, "Insufficient house permission")

    def house_for(session: Session, kind: str, entity_id: str) -> str:
        if kind == "house":
            HouseRepository(session).get(entity_id)
            return entity_id
        if kind == "floor":
            return FloorRepository(session).get(entity_id).house_id
        if kind == "room":
            return RoomRepository(session).house_id(entity_id)
        if kind == "device":
            return DeviceRepository(session).house_id(entity_id)
        if kind == "scene":
            return SceneRepository(session).get(entity_id).house_id
        return AutomationRepository(session).get(entity_id).house_id

    def allowed(session: Session, user: UserORM, permission: Permission) -> list[str]:
        return authorization(session).accessible_house_ids(user.id, permission)

    get_current_user = build_current_user_dependency(get_session, codec)

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
            DeviceRepository(session), RoomRepository(session), event_bus, transport
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
        return {"status": "ok", "version": "0.6.0b1"}

    @router.post("/auth/login", response_model=TokenResponse)
    async def login(data: LoginRequest, session: Session = Depends(get_session)):
        try:
            result = auth_service(session).login(str(data.email), data.password)
        except AuthenticationError as error:
            await event_bus.publish(Event(type="auth_login_failed", data={}))
            raise HTTPException(
                401, str(error), headers={"WWW-Authenticate": "Bearer"}
            ) from error
        await event_bus.publish(Event(type="auth_login_succeeded", data={}))
        return result

    @router.post("/auth/refresh", response_model=TokenResponse)
    async def refresh(data: TokenRequest, session: Session = Depends(get_session)):
        try:
            result = auth_service(session).refresh(data.refresh_token)
        except AuthenticationError as error:
            await event_bus.publish(Event(type="auth_refresh_failed", data={}))
            raise HTTPException(401, str(error)) from error
        await event_bus.publish(Event(type="auth_refresh_succeeded", data={}))
        return result

    @router.post("/auth/logout", status_code=204)
    async def logout(data: TokenRequest, session: Session = Depends(get_session)):
        try:
            auth_service(session).logout(data.refresh_token)
        except AuthenticationError as error:
            raise HTTPException(401, str(error)) from error
        await event_bus.publish(Event(type="auth_logout", data={}))
        return Response(status_code=204)

    @router.get("/auth/me", response_model=UserRead)
    def me(user=Depends(get_current_user)):
        return user

    @router.post("/houses", response_model=HouseRead, status_code=201)
    def create_house(
        data: HouseCreate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        return HouseRead.model_validate(
            HouseRepository(session).create_with_owner(data.model_dump(), user.id)
        )

    @router.get("/houses", response_model=list[HouseRead])
    def list_houses(
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        return HouseRepository(session).for_ids(
            allowed(session, user, Permission.HOUSE_READ)
        )

    @router.get("/houses/{entity_id}", response_model=HouseRead)
    def get_house(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, entity_id, Permission.HOUSE_READ)
        return structure(session, "house").get(entity_id)

    @router.patch("/houses/{entity_id}", response_model=HouseRead)
    def update_house(
        entity_id: str,
        data: HouseUpdate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, entity_id, Permission.HOUSE_MANAGE)
        return structure(session, "house").update(entity_id, data)

    @router.delete("/houses/{entity_id}", status_code=204)
    def delete_house(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, entity_id, Permission.HOUSE_MANAGE)
        HouseRepository(session).delete_with_memberships(entity_id)
        return Response(status_code=204)

    @router.post("/floors", response_model=FloorRead, status_code=201)
    def create_floor(
        data: FloorCreate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, data.house_id, Permission.HOUSE_MANAGE)
        return structure(session, "floor").create(data)

    @router.get("/floors", response_model=list[FloorRead])
    def list_floors(
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        return FloorRepository(session).for_houses(
            allowed(session, user, Permission.HOUSE_READ)
        )

    @router.get("/floors/{entity_id}", response_model=FloorRead)
    def get_floor(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session, user, house_for(session, "floor", entity_id), Permission.HOUSE_READ
        )
        return structure(session, "floor").get(entity_id)

    @router.patch("/floors/{entity_id}", response_model=FloorRead)
    def update_floor(
        entity_id: str,
        data: FloorUpdate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "floor", entity_id),
            Permission.HOUSE_MANAGE,
        )
        if data.house_id is not None:
            require(session, user, data.house_id, Permission.HOUSE_MANAGE)
        return structure(session, "floor").update(entity_id, data)

    @router.delete("/floors/{entity_id}", status_code=204)
    def delete_floor(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "floor", entity_id),
            Permission.HOUSE_MANAGE,
        )
        structure(session, "floor").delete(entity_id)
        return Response(status_code=204)

    @router.post("/rooms", response_model=RoomRead, status_code=201)
    def create_room(
        data: RoomCreate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "floor", data.floor_id),
            Permission.HOUSE_MANAGE,
        )
        return structure(session, "room").create(data)

    @router.get("/rooms", response_model=list[RoomRead])
    def list_rooms(
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        return RoomRepository(session).for_houses(
            allowed(session, user, Permission.HOUSE_READ)
        )

    @router.get("/rooms/{entity_id}", response_model=RoomRead)
    def get_room(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session, user, house_for(session, "room", entity_id), Permission.HOUSE_READ
        )
        return structure(session, "room").get(entity_id)

    @router.patch("/rooms/{entity_id}", response_model=RoomRead)
    def update_room(
        entity_id: str,
        data: RoomUpdate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "room", entity_id),
            Permission.HOUSE_MANAGE,
        )
        if data.floor_id is not None:
            require(
                session,
                user,
                house_for(session, "floor", data.floor_id),
                Permission.HOUSE_MANAGE,
            )
        return structure(session, "room").update(entity_id, data)

    @router.delete("/rooms/{entity_id}", status_code=204)
    def delete_room(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "room", entity_id),
            Permission.HOUSE_MANAGE,
        )
        structure(session, "room").delete(entity_id)
        return Response(status_code=204)

    @router.post("/devices", response_model=DeviceRead, status_code=201)
    def create_device(
        data: DeviceCreate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "room", data.room_id),
            Permission.DEVICE_MANAGE,
        )
        return device_service(session).create(data)

    @router.get("/devices", response_model=list[DeviceRead])
    def list_devices(
        house_id: str | None = None,
        room_id: str | None = None,
        type: DeviceType | None = Query(None),
        online: bool | None = None,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        house_ids = allowed(session, user, Permission.DEVICE_READ)
        if house_id is not None:
            require(session, user, house_id, Permission.DEVICE_READ)
            house_ids = [house_id]
        if room_id is not None:
            require(
                session,
                user,
                house_for(session, "room", room_id),
                Permission.DEVICE_READ,
            )
        return DeviceRepository(session).filtered_for_houses(
            house_ids, room_id, type, online
        )

    @router.get("/devices/{entity_id}", response_model=DeviceRead)
    def get_device(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "device", entity_id),
            Permission.DEVICE_READ,
        )
        return device_service(session).get(entity_id)

    @router.patch("/devices/{entity_id}", response_model=DeviceRead)
    def update_device(
        entity_id: str,
        data: DeviceUpdate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "device", entity_id),
            Permission.DEVICE_MANAGE,
        )
        if data.room_id is not None:
            require(
                session,
                user,
                house_for(session, "room", data.room_id),
                Permission.DEVICE_MANAGE,
            )
        return device_service(session).update(entity_id, data)

    @router.delete("/devices/{entity_id}", status_code=204)
    def delete_device(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "device", entity_id),
            Permission.DEVICE_MANAGE,
        )
        device_service(session).delete(entity_id)
        return Response(status_code=204)

    @router.patch("/devices/{entity_id}/state", response_model=DeviceRead)
    async def update_state(
        entity_id: str,
        state: DeviceState,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "device", entity_id),
            Permission.DEVICE_CONTROL,
        )
        return await device_service(session).update_state(entity_id, state)

    @router.post("/devices/{entity_id}/on", response_model=DeviceRead)
    async def turn_on(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "device", entity_id),
            Permission.DEVICE_CONTROL,
        )
        return await device_service(session).update_state(entity_id, {"on": True})

    @router.post("/devices/{entity_id}/off", response_model=DeviceRead)
    async def turn_off(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "device", entity_id),
            Permission.DEVICE_CONTROL,
        )
        return await device_service(session).update_state(entity_id, {"on": False})

    @router.post("/scenes", response_model=SceneRead, status_code=201)
    def create_scene(
        data: SceneCreate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, data.house_id, Permission.SCENE_MANAGE)
        return scene_service(session).create(data)

    @router.get("/scenes", response_model=list[SceneRead])
    def list_scenes(
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        return SceneRepository(session).for_houses(
            allowed(session, user, Permission.SCENE_READ)
        )

    @router.get("/scenes/{entity_id}", response_model=SceneRead)
    def get_scene(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session, user, house_for(session, "scene", entity_id), Permission.SCENE_READ
        )
        return scene_service(session).get(entity_id)

    @router.patch("/scenes/{entity_id}", response_model=SceneRead)
    def update_scene(
        entity_id: str,
        data: SceneUpdate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "scene", entity_id),
            Permission.SCENE_MANAGE,
        )
        if data.house_id is not None:
            require(session, user, data.house_id, Permission.SCENE_MANAGE)
        return scene_service(session).update(entity_id, data)

    @router.delete("/scenes/{entity_id}", status_code=204)
    def delete_scene(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "scene", entity_id),
            Permission.SCENE_MANAGE,
        )
        scene_service(session).delete(entity_id)
        return Response(status_code=204)

    @router.post("/scenes/{entity_id}/run", response_model=SceneRead)
    async def run_scene(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session, user, house_for(session, "scene", entity_id), Permission.SCENE_RUN
        )
        return await scene_service(session).run(entity_id)

    @router.post("/automations", response_model=AutomationRead, status_code=201)
    def create_automation(
        data: AutomationCreate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, data.house_id, Permission.AUTOMATION_MANAGE)
        return automation_service(session).create(data)

    @router.get("/automations", response_model=list[AutomationRead])
    def list_automations(
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        return AutomationRepository(session).for_houses(
            allowed(session, user, Permission.AUTOMATION_READ)
        )

    @router.get("/automations/{entity_id}", response_model=AutomationRead)
    def get_automation(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "automation", entity_id),
            Permission.AUTOMATION_READ,
        )
        return automation_service(session).get(entity_id)

    @router.patch("/automations/{entity_id}", response_model=AutomationRead)
    def update_automation(
        entity_id: str,
        data: AutomationUpdate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "automation", entity_id),
            Permission.AUTOMATION_MANAGE,
        )
        if data.house_id is not None:
            require(session, user, data.house_id, Permission.AUTOMATION_MANAGE)
        return automation_service(session).update(entity_id, data)

    @router.delete("/automations/{entity_id}", status_code=204)
    def delete_automation(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "automation", entity_id),
            Permission.AUTOMATION_MANAGE,
        )
        automation_service(session).delete(entity_id)
        return Response(status_code=204)

    @router.post("/automations/{entity_id}/enable", response_model=AutomationRead)
    def enable_automation(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "automation", entity_id),
            Permission.AUTOMATION_MANAGE,
        )
        return automation_service(session).set_enabled(entity_id, True)

    @router.post("/automations/{entity_id}/disable", response_model=AutomationRead)
    def disable_automation(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "automation", entity_id),
            Permission.AUTOMATION_MANAGE,
        )
        return automation_service(session).set_enabled(entity_id, False)

    @router.post("/automations/{entity_id}/run", response_model=AutomationRead)
    async def run_automation(
        entity_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(
            session,
            user,
            house_for(session, "automation", entity_id),
            Permission.AUTOMATION_MANAGE,
        )
        return await automation_service(session).manual_run(entity_id)

    @router.get("/events", response_model=list[EventLogRead])
    def list_events(
        house_id: str | None = None,
        event_type: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        permission = (
            Permission.SECURITY_AUDIT_READ
            if event_type is not None
            and event_type.startswith(("auth_", "authorization_"))
            else Permission.EVENT_READ
        )
        house_ids = allowed(session, user, permission)
        if house_id is not None:
            require(session, user, house_id, permission)
            house_ids = [house_id]
        return EventLogRepository(session).filtered_for_houses(
            house_ids,
            event_type,
            limit,
            allowed(session, user, Permission.SECURITY_AUDIT_READ),
        )

    @router.get("/houses/{house_id}/members", response_model=list[MembershipRead])
    def list_members(
        house_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.MEMBER_READ)
        return MembershipService(
            MembershipRepository(session), UserRepository(session)
        ).list(house_id)

    @router.post(
        "/houses/{house_id}/members", response_model=MembershipRead, status_code=201
    )
    def add_member(
        house_id: str,
        data: MembershipCreate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.MEMBER_MANAGE)
        return MembershipService(
            MembershipRepository(session), UserRepository(session)
        ).add(house_id, data.user_id, data.role)

    @router.patch("/houses/{house_id}/members/{user_id}", response_model=MembershipRead)
    def change_member(
        house_id: str,
        user_id: str,
        data: MembershipUpdate,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.MEMBER_MANAGE)
        return MembershipService(
            MembershipRepository(session), UserRepository(session)
        ).change(house_id, user_id, data.role)

    @router.delete("/houses/{house_id}/members/{user_id}", status_code=204)
    def remove_member(
        house_id: str,
        user_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.MEMBER_MANAGE)
        MembershipService(
            MembershipRepository(session), UserRepository(session)
        ).remove(house_id, user_id)
        return Response(status_code=204)

    return router
