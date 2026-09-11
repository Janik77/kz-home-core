from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DeviceORM, FloorORM, RoomORM
from app.repositories.base import Repository


class DeviceRepository(Repository[DeviceORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, DeviceORM)

    def filtered(
        self,
        house_id: str | None,
        room_id: str | None,
        device_type: str | None,
        online: bool | None,
    ) -> list[DeviceORM]:
        statement = select(DeviceORM)
        if house_id is not None:
            statement = (
                statement.join(DeviceORM.room)
                .join(RoomORM.floor)
                .where(FloorORM.house_id == house_id)
            )
        if room_id is not None:
            statement = statement.where(DeviceORM.room_id == room_id)
        if device_type is not None:
            statement = statement.where(DeviceORM.type == device_type)
        if online is not None:
            statement = statement.where(DeviceORM.online == online)
        return list(self.session.scalars(statement).all())

    def house_id(self, device_id: str) -> str:
        statement = (
            select(FloorORM.house_id)
            .join(RoomORM, RoomORM.floor_id == FloorORM.id)
            .join(DeviceORM, DeviceORM.room_id == RoomORM.id)
            .where(DeviceORM.id == device_id)
        )
        house_id = self.session.scalar(statement)
        if house_id is None:
            self.get(device_id)
        return house_id  # type: ignore[return-value]
