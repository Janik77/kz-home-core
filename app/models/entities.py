from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HouseORM(TimestampMixin, Base):
    __tablename__ = "houses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    floors: Mapped[list["FloorORM"]] = relationship(
        back_populates="house", cascade="all, delete-orphan"
    )
    scenes: Mapped[list["SceneORM"]] = relationship(
        back_populates="house", cascade="all, delete-orphan"
    )
    automations: Mapped[list["AutomationORM"]] = relationship(
        back_populates="house", cascade="all, delete-orphan"
    )


class UserORM(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)


class HouseMembershipORM(TimestampMixin, Base):
    __tablename__ = "house_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'installer', 'technician', 'resident')",
            name="ck_membership_role",
        ),
        UniqueConstraint("user_id", "house_id", name="uq_membership_user_house"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    house_id: Mapped[str] = mapped_column(
        ForeignKey("houses.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))


class RefreshSessionORM(Base):
    __tablename__ = "refresh_sessions"
    jti_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FloorORM(TimestampMixin, Base):
    __tablename__ = "floors"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    house_id: Mapped[str] = mapped_column(
        ForeignKey("houses.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    order: Mapped[int] = mapped_column(Integer)
    house: Mapped[HouseORM] = relationship(back_populates="floors")
    rooms: Mapped[list["RoomORM"]] = relationship(
        back_populates="floor", cascade="all, delete-orphan"
    )


class RoomORM(TimestampMixin, Base):
    __tablename__ = "rooms"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    floor_id: Mapped[str] = mapped_column(
        ForeignKey("floors.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    icon: Mapped[str | None] = mapped_column(String(100), nullable=True)
    floor: Mapped[FloorORM] = relationship(back_populates="rooms")
    devices: Mapped[list["DeviceORM"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )


class DeviceORM(TimestampMixin, Base):
    __tablename__ = "devices"
    __table_args__ = (Index("ix_devices_type", "type"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    room_id: Mapped[str] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(50))
    state: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    capabilities: Mapped[list[str]] = mapped_column(JsonType, default=list)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JsonType, default=dict
    )
    online: Mapped[bool] = mapped_column(Boolean, default=True)
    room: Mapped[RoomORM] = relationship(back_populates="devices")


class SceneORM(TimestampMixin, Base):
    __tablename__ = "scenes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    house_id: Mapped[str] = mapped_column(
        ForeignKey("houses.id", ondelete="CASCADE"), index=True
    )
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    house: Mapped[HouseORM] = relationship(back_populates="scenes")


class AutomationORM(TimestampMixin, Base):
    __tablename__ = "automations"
    __table_args__ = (Index("ix_automations_house_enabled", "house_id", "enabled"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    house_id: Mapped[str] = mapped_column(
        ForeignKey("houses.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trigger: Mapped[dict[str, Any]] = mapped_column(JsonType)
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    house: Mapped[HouseORM] = relationship(back_populates="automations")


class EventLogORM(Base):
    __tablename__ = "event_logs"
    __table_args__ = (
        Index("ix_event_logs_house_created", "house_id", "created_at"),
        Index("ix_event_logs_type", "event_type"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    house_id: Mapped[str | None] = mapped_column(
        ForeignKey("houses.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
