from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DeviceORM, FloorORM, PhysicalDeviceORM, RoomORM
from app.repositories.base import Repository


class DeviceRepository(Repository[DeviceORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, DeviceORM)

    def get_current(self, device_id: str) -> DeviceORM:
        # Refresh after lifecycle admission locks, including any cached ORM row.
        entity = self.session.get(DeviceORM, device_id, populate_existing=True)
        if entity is None:
            return self.get(device_id)  # Preserve the existing not-found error.
        return entity

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
                .outerjoin(
                    PhysicalDeviceORM, PhysicalDeviceORM.bound_device_id == DeviceORM.id
                )
                .where(
                    func.coalesce(PhysicalDeviceORM.house_id, FloorORM.house_id)
                    == house_id
                )
            )
        if room_id is not None:
            statement = statement.where(DeviceORM.room_id == room_id)
        if device_type is not None:
            statement = statement.where(DeviceORM.type == device_type)
        if online is not None:
            statement = statement.where(DeviceORM.online == online)
        return list(self.session.scalars(statement).all())

    def filtered_for_houses(
        self,
        house_ids: list[str],
        room_id: str | None,
        device_type: str | None,
        online: bool | None,
    ) -> list[DeviceORM]:
        if not house_ids:
            return []
        statement = (
            select(DeviceORM)
            .join(DeviceORM.room)
            .join(RoomORM.floor)
            .outerjoin(
                PhysicalDeviceORM, PhysicalDeviceORM.bound_device_id == DeviceORM.id
            )
            .where(
                func.coalesce(PhysicalDeviceORM.house_id, FloorORM.house_id).in_(
                    house_ids
                )
            )
        )
        if room_id is not None:
            statement = statement.where(DeviceORM.room_id == room_id)
        if device_type is not None:
            statement = statement.where(DeviceORM.type == device_type)
        if online is not None:
            statement = statement.where(DeviceORM.online == online)
        return list(self.session.scalars(statement).all())

    def house_id(self, device_id: str) -> str:
        # A physical binding is the authority even if location data is corrupted.
        bound_house = self.session.scalar(
            select(PhysicalDeviceORM.house_id).where(
                PhysicalDeviceORM.bound_device_id == device_id
            )
        )
        if bound_house is not None:
            return bound_house
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
