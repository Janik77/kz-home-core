from contextlib import contextmanager

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, EntityNotFoundError
from app.models import (
    DeviceORM,
    EventLogORM,
    FloorORM,
    HouseORM,
    PhysicalDeviceORM,
    RoomORM,
)


class PhysicalDeviceRepository:
    def __init__(self, session: Session):
        self.session = session

    @contextmanager
    def atomic(self):
        """Join request reads; commit exactly once, including audit and device creation."""
        try:
            yield
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            raise ConflictError(
                "Physical device operation conflicts with existing data"
            ) from None
        except Exception:
            self.session.rollback()
            raise

    def lock_houses(self, house_ids: list[str]) -> None:
        # Shared with structural mutations; canonical order avoids lock inversion.
        list(
            self.session.scalars(
                select(HouseORM)
                .where(HouseORM.id.in_(house_ids))
                .order_by(HouseORM.id)
                .with_for_update()
            )
        )

    def by_hardware(self, hardware_id: str) -> PhysicalDeviceORM | None:
        return self.session.scalar(
            select(PhysicalDeviceORM)
            .where(PhysicalDeviceORM.hardware_id == hardware_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def by_device(
        self, device_id: str, *, lock: bool = False
    ) -> PhysicalDeviceORM | None:
        statement = select(PhysicalDeviceORM).where(
            PhysicalDeviceORM.device_id == device_id
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement.execution_options(populate_existing=True))

    def for_house(self, house_id: str, device_id: str) -> PhysicalDeviceORM:
        item = self.by_device(device_id, lock=True)
        if item is None or item.house_id != house_id:
            raise EntityNotFoundError("Resource not found")
        return item

    def list_for_house(self, house_id: str) -> list[PhysicalDeviceORM]:
        return list(
            self.session.scalars(
                select(PhysicalDeviceORM)
                .where(PhysicalDeviceORM.house_id == house_id)
                .order_by(PhysicalDeviceORM.device_id)
            )
        )

    def add_inventory(self, values: dict) -> PhysicalDeviceORM:
        entity = PhysicalDeviceORM(**values)
        self.session.add(entity)
        self.session.flush()
        return entity

    def claim(self, item: PhysicalDeviceORM, house_id: str) -> None:
        result = self.session.execute(
            update(PhysicalDeviceORM)
            .where(
                PhysicalDeviceORM.device_id == item.device_id,
                PhysicalDeviceORM.status == "unprovisioned",
                PhysicalDeviceORM.house_id.is_(None),
            )
            .values(
                house_id=house_id,
                bound_device_id=item.device_id,
                claim_code_hash=None,
                status="provisioning",
            )
        )
        if result.rowcount != 1:
            raise ConflictError("Physical device is already claimed")

    def add_device(self, values: dict) -> None:
        self.session.add(DeviceORM(**values))
        self.session.flush()

    def set_status(self, item: PhysicalDeviceORM, status: str) -> None:
        previous = item.status
        result = self.session.execute(
            update(PhysicalDeviceORM)
            .where(
                PhysicalDeviceORM.device_id == item.device_id,
                PhysicalDeviceORM.status == previous,
            )
            .values(status=status)
        )
        if result.rowcount != 1:
            raise ConflictError("Physical lifecycle changed concurrently; retry")
        self.session.execute(
            update(DeviceORM).where(DeviceORM.id == item.device_id).values(online=False)
        )

    def audit(self, values: dict) -> None:
        self.session.add(EventLogORM(**values))
        self.session.flush()

    def bound_in_structure(self, kind: str, entity_id: str) -> bool:
        statement = select(PhysicalDeviceORM.device_id).where(
            PhysicalDeviceORM.house_id.is_not(None)
        )
        if kind == "house":
            statement = statement.where(PhysicalDeviceORM.house_id == entity_id)
        else:
            statement = statement.join(
                DeviceORM, DeviceORM.id == PhysicalDeviceORM.bound_device_id
            )
            if kind == "device":
                statement = statement.where(DeviceORM.id == entity_id)
            elif kind == "room":
                statement = statement.where(DeviceORM.room_id == entity_id)
            else:
                statement = statement.join(RoomORM, RoomORM.id == DeviceORM.room_id)
                statement = statement.where(RoomORM.floor_id == entity_id)
        return self.session.scalar(statement.limit(1)) is not None

    def structure_house(self, kind: str, entity_id: str) -> str:
        if kind == "house":
            statement = select(HouseORM.id).where(HouseORM.id == entity_id)
        elif kind == "floor":
            statement = select(FloorORM.house_id).where(FloorORM.id == entity_id)
        elif kind == "room":
            statement = (
                select(FloorORM.house_id).join(RoomORM).where(RoomORM.id == entity_id)
            )
        else:
            statement = (
                select(FloorORM.house_id)
                .join(RoomORM)
                .join(DeviceORM)
                .where(DeviceORM.id == entity_id)
            )
        house_id = self.session.scalar(statement)
        if house_id is None:
            raise EntityNotFoundError("Resource not found")
        return house_id
