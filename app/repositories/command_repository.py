from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, EntityNotFoundError
from app.models import DeviceCommandORM, DeviceORM

PENDING = ("queued", "dispatched")


class CommandRepository:
    def __init__(self, session: Session):
        self.session = session

    def lock_device(self, device_id: str) -> DeviceORM:
        row = self.session.scalar(
            select(DeviceORM)
            .where(DeviceORM.id == device_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise EntityNotFoundError("Resource not found")
        return row

    def find(self, **fields) -> DeviceCommandORM | None:
        statement = (
            select(DeviceCommandORM)
            .filter_by(**fields)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self.session.scalar(statement)

    def pending(self, device_id: str) -> DeviceCommandORM | None:
        return self.session.scalar(
            select(DeviceCommandORM)
            .where(
                DeviceCommandORM.device_id == device_id,
                DeviceCommandORM.status.in_(PENDING),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def save(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise ConflictError("Command conflicts with an existing request") from error
