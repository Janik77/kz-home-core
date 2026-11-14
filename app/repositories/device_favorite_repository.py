from sqlalchemy import and_, delete, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, EntityNotFoundError
from app.models import (
    DeviceFavoriteORM,
    DeviceORM,
    FloorORM,
    HouseMembershipORM,
    RoomORM,
    UserORM,
    PhysicalDeviceORM,
)
from app.security import Permission, ROLE_PERMISSIONS


class DeviceFavoriteRepository:
    def __init__(self, session):
        self.session = session

    def readable(self, user_id, house_id):
        # Match DeviceRepository's authoritative physical binding, not metadata
        # or an accidentally corrupted room hierarchy. Recheck current membership
        # in the SQL statement, not just an earlier HTTP authorization check.
        return (
            select(DeviceORM.id)
            .join(RoomORM, RoomORM.id == DeviceORM.room_id)
            .join(FloorORM, FloorORM.id == RoomORM.floor_id)
            .outerjoin(
                PhysicalDeviceORM, PhysicalDeviceORM.bound_device_id == DeviceORM.id
            )
            .join(
                HouseMembershipORM,
                and_(
                    HouseMembershipORM.house_id == house_id,
                    HouseMembershipORM.user_id == user_id,
                ),
            )
            .join(UserORM, UserORM.id == HouseMembershipORM.user_id)
            .where(
                func.coalesce(PhysicalDeviceORM.house_id, FloorORM.house_id)
                == house_id,
                UserORM.is_active.is_(True),
                HouseMembershipORM.role.in_(
                    [
                        role
                        for role, rights in ROLE_PERMISSIONS.items()
                        if Permission.DEVICE_READ in rights
                    ]
                ),
            )
        )

    def list(self, user_id, house_id):
        return list(
            self.session.scalars(
                self.readable(user_id, house_id)
                .join(
                    DeviceFavoriteORM,
                    and_(
                        DeviceFavoriteORM.device_id == DeviceORM.id,
                        DeviceFavoriteORM.user_id == user_id,
                        DeviceFavoriteORM.house_id == house_id,
                    ),
                )
                .order_by(DeviceORM.id)
            )
        )

    def put(self, user_id, house_id, device_id):
        dialect = self.session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        # A single INSERT SELECT admits only a device visible in this house.
        source = (
            self.readable(user_id, house_id)
            .where(DeviceORM.id == device_id)
            .with_only_columns(
                literal(user_id),
                literal(house_id),
                DeviceORM.id,
                maintain_column_froms=True,
            )
        )
        statement = insert(DeviceFavoriteORM).from_select(
            ["user_id", "house_id", "device_id"], source
        )
        statement = statement.on_conflict_do_nothing(
            index_elements=["user_id", "house_id", "device_id"]
        )
        try:
            self.session.execute(statement)
            # ON CONFLICT may be a successful repeat; distinguish absent/foreign
            # devices without exposing another user's favorite or another house.
            if (
                self.session.scalar(
                    self.readable(user_id, house_id).where(DeviceORM.id == device_id)
                )
                is None
            ):
                self.session.rollback()
                raise EntityNotFoundError("Resource not found")
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise ConflictError(
                "Favorite target or membership changed concurrently"
            ) from error

    def delete(self, user_id, house_id, device_id):
        # Remove only this user's preference, even after move/deletion. No device
        # existence probe: repeated DELETE remains 204 and cannot enumerate IDs.
        self.session.execute(
            delete(DeviceFavoriteORM).where(
                DeviceFavoriteORM.user_id == user_id,
                DeviceFavoriteORM.house_id == house_id,
                DeviceFavoriteORM.device_id == device_id,
            )
        )
        self.session.commit()
