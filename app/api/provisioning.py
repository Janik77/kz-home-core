"""Human session administration only; no proof/confirm/enrollment endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.onboarding import PrivateValidationRoute
from app.models import UserORM
from app.schemas.onboarding import TopicID
from app.schemas.provisioning import (
    ProvisioningStart,
    ProvisioningCancel,
    ProvisioningPublicStatus,
)
from app.security import Permission
from app.services.provisioning_service import ProvisioningService


def build_provisioning_router(
    get_session: Any, get_user: Any, require: Any
) -> APIRouter:
    router = APIRouter(route_class=PrivateValidationRoute)

    def execute(session, user, house, response, operation):
        response.headers["Cache-Control"] = "no-store"
        try:
            require(session, user, house, Permission.DEVICE_ONBOARD)
            return operation(ProvisioningService(session))
        except SQLAlchemyError:
            session.rollback()
            raise HTTPException(503, "Provisioning storage unavailable") from None

    @router.post(
        "/houses/{house_id}/provisioning-sessions",
        response_model=ProvisioningPublicStatus,
        status_code=201,
    )
    def create(
        house_id: TopicID,
        data: ProvisioningStart,
        response: Response,
        idempotency_key: UUID = Header(),
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        return execute(
            session,
            user,
            house_id,
            response,
            lambda service: service.create(house_id, user.id, data, idempotency_key),
        )

    @router.get(
        "/houses/{house_id}/provisioning-sessions/{session_id}",
        response_model=ProvisioningPublicStatus,
    )
    def get(
        house_id: TopicID,
        session_id: UUID,
        response: Response,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        return execute(
            session,
            user,
            house_id,
            response,
            lambda service: service.get(house_id, user.id, session_id),
        )

    @router.post(
        "/houses/{house_id}/provisioning-sessions/{session_id}/cancel",
        response_model=ProvisioningPublicStatus,
    )
    def cancel(
        house_id: TopicID,
        session_id: UUID,
        data: ProvisioningCancel,
        response: Response,
        idempotency_key: UUID = Header(),
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_user),
    ):
        return execute(
            session,
            user,
            house_id,
            response,
            lambda service: service.cancel(
                house_id, user.id, session_id, data.expected_revision, idempotency_key
            ),
        )

    return router
