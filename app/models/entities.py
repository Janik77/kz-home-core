from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, func
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
