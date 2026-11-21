"""Owner/installer enrollment of explicitly provisioned software-lab identities."""

from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.onboarding import PrivateValidationRoute
from app.core.errors import ConflictError, EntityNotFoundError, UnavailableError
from app.models import DeviceORM, EventLogORM, PhysicalDeviceORM
from app.repositories import DeviceRepository, RoomRepository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas import DeviceRead
from app.schemas.onboarding import TopicID
from app.security import Permission

PROFILES = {"relay": ["on_off"], "temperature_sensor": ["temperature"]}
MARKER = {"software_lab_enrollment": "v1"}


class Candidate(BaseModel):
    device_id: TopicID
    device_type: Literal["relay", "temperature_sensor"]


class ClaimCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    room_id: TopicID
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]


def build_software_onboarding_router(
    get_session, get_user, require, lock_scope, settings, read
):
    router = APIRouter(route_class=PrivateValidationRoute)
    candidates = {
        (house, device): profile
        for house, device, profile in settings.software_device_candidates
    }
    prefix = "/v1/houses/{house_id}/software-device-candidates"

    def check(session, user, house_id, response):
        response.headers["Cache-Control"] = "no-store"
        if not candidates:
            raise EntityNotFoundError("Resource not found")
        require(session, user, house_id, Permission.DEVICE_ONBOARD)

    @router.get(prefix, response_model=list[Candidate])
    def list_candidates(
        house_id: TopicID,
        response: Response,
        session=Depends(get_session),
        user=Depends(get_user),
    ):
        try:
            check(session, user, house_id, response)
            keys = [device for house, device in candidates if house == house_id]
            if not keys:
                return []
            occupied = set(
                session.scalars(select(DeviceORM.id).where(DeviceORM.id.in_(keys)))
            )
            occupied.update(
                session.scalars(
                    select(PhysicalDeviceORM.device_id).where(
                        PhysicalDeviceORM.device_id.in_(keys)
                    )
                )
            )
            return [
                Candidate(device_id=key, device_type=candidates[house_id, key])
                for key in sorted(keys)
                if key not in occupied
            ]
        except SQLAlchemyError:
            session.rollback()
            raise UnavailableError(
                "Software device enrollment is temporarily unavailable"
            ) from None

    @router.post(
        prefix + "/{device_id}/claim", response_model=DeviceRead, status_code=201
    )
    def claim(
        house_id: TopicID,
        device_id: TopicID,
        data: ClaimCandidate,
        response: Response,
        session=Depends(get_session),
        user=Depends(get_user),
    ):
        try:
            check(session, user, house_id, response)
            profile = candidates.get((house_id, device_id))
            rooms = RoomRepository(session)
            if profile is None or rooms.house_id(data.room_id) != house_id:
                raise EntityNotFoundError("Resource not found")
            lock_scope(
                session,
                user,
                "room",
                data.room_id,
                permission=Permission.DEVICE_ONBOARD,
            )
            if rooms.house_id(data.room_id) != house_id:
                raise EntityNotFoundError("Resource not found")
            physical = PhysicalDeviceRepository(session)
            if physical.by_device(device_id, lock=True) is not None:
                raise EntityNotFoundError("Resource not found")
            devices = DeviceRepository(session)
            existing = session.get(DeviceORM, device_id, populate_existing=True)
            if existing is not None:
                if devices.house_id(device_id) != house_id:
                    raise EntityNotFoundError("Resource not found")
                if (
                    existing.metadata_.get("software_lab_enrollment") != "v1"
                    or existing.type != profile
                    or existing.capabilities != PROFILES[profile]
                    or existing.room_id != data.room_id
                    or existing.name != data.name
                ):
                    raise ConflictError("Software device is already registered; reload")
                return read(session, device_id)

            session.add(
                DeviceORM(
                    id=device_id,
                    room_id=data.room_id,
                    name=data.name,
                    type=profile,
                    state={},
                    online=False,
                    capabilities=PROFILES[profile],
                    metadata_=MARKER,
                )
            )
            session.add(
                EventLogORM(
                    id=str(uuid4()),
                    house_id=house_id,
                    entity_id=device_id,
                    event_type="software_device_enrolled",
                    correlation_id=str(uuid4()),
                    payload={
                        "device_id": device_id,
                        "actor_id": user.id,
                        "room_id": data.room_id,
                    },
                )
            )
            session.flush()
            result = read(session, device_id)
            session.commit()
            return result
        except SQLAlchemyError:
            session.rollback()
            raise UnavailableError(
                "Software device enrollment is temporarily unavailable"
            ) from None

    return router
