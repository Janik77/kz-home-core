from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.models import UserORM
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.onboarding import (
    DeviceActivation,
    DeviceClaim,
    PhysicalDeviceRead,
    TopicID,
)
from app.security import Permission
from app.services.onboarding_service import OnboardingService


class PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Default validation responses include rejected input and may echo
                # claim codes or accidental credential fields. Never return them.
                raise HTTPException(422, "Invalid onboarding request") from None

        return safe_handler


def build_onboarding_router(
    get_session: Any, get_user: Any, require: Callable
) -> APIRouter:
    router = APIRouter(route_class=PrivateValidationRoute)

    @router.post(
        "/houses/{house_id}/device-claims",
        response_model=PhysicalDeviceRead,
        status_code=201,
    )
    def claim(
        house_id: TopicID,
        data: DeviceClaim,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        require(session, user, house_id, Permission.DEVICE_ONBOARD)
        return OnboardingService(PhysicalDeviceRepository(session)).claim(
            house_id, data, user.id
        )

    @router.get(
        "/houses/{house_id}/physical-devices", response_model=list[PhysicalDeviceRead]
    )
    def list_physical(
        house_id: TopicID,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        require(session, user, house_id, Permission.DEVICE_READ)
        return PhysicalDeviceRepository(session).list_for_house(house_id)

    @router.get(
        "/houses/{house_id}/physical-devices/{device_id}",
        response_model=PhysicalDeviceRead,
    )
    def get_physical(
        house_id: TopicID,
        device_id: TopicID,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        require(session, user, house_id, Permission.DEVICE_READ)
        return PhysicalDeviceRepository(session).for_house(house_id, device_id)

    @router.post(
        "/houses/{house_id}/physical-devices/{device_id}/activate",
        response_model=PhysicalDeviceRead,
    )
    def activate(
        house_id: TopicID,
        device_id: TopicID,
        _data: DeviceActivation,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        require(session, user, house_id, Permission.DEVICE_ONBOARD)
        return OnboardingService(PhysicalDeviceRepository(session)).transition(
            house_id, device_id, "active", user.id
        )

    @router.post(
        "/houses/{house_id}/physical-devices/{device_id}/deactivate",
        response_model=PhysicalDeviceRead,
    )
    def deactivate(
        house_id: TopicID,
        device_id: TopicID,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        require(session, user, house_id, Permission.DEVICE_ONBOARD)
        return OnboardingService(PhysicalDeviceRepository(session)).transition(
            house_id, device_id, "inactive", user.id
        )

    @router.post(
        "/houses/{house_id}/physical-devices/{device_id}/revoke",
        response_model=PhysicalDeviceRead,
    )
    def revoke(
        house_id: TopicID,
        device_id: TopicID,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        require(session, user, house_id, Permission.DEVICE_ONBOARD)
        return OnboardingService(PhysicalDeviceRepository(session)).transition(
            house_id, device_id, "revoked", user.id
        )

    return router
