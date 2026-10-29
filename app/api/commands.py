from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.models import UserORM
from app.repositories.command_repository import CommandRepository
from app.repositories.device_repository import DeviceRepository
from app.schemas.commands import CommandRead, RelayCommandCreate
from app.security import Permission
from app.services.command_service import CommandService


def build_command_router(get_session, get_current_user, require, transport):
    router = APIRouter(prefix="/v1")

    @router.post(
        "/devices/{device_id}/commands", response_model=CommandRead, status_code=202
    )
    async def submit_command(
        device_id: str,
        data: RelayCommandCreate,
        idempotency_key: str = Header(
            ..., min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
        ),
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        house_id = DeviceRepository(session).house_id(device_id)
        require(session, user, house_id, Permission.DEVICE_CONTROL)
        return await CommandService(CommandRepository(session), transport).submit(
            house_id, device_id, user.id, idempotency_key, data.on
        )

    @router.get("/houses/{house_id}/commands/{command_id}", response_model=CommandRead)
    def get_command(
        house_id: str,
        command_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.DEVICE_READ)
        return CommandService(CommandRepository(session)).get(house_id, command_id)

    return router
