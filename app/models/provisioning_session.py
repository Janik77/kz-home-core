"""Durable attempts, never a second device or ownership binding."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProvisioningSessionORM(Base):
    __tablename__ = "provisioning_sessions"
    __table_args__ = (
        UniqueConstraint(
            "house_id", "actor_id", "create_key_hash", name="uq_provisioning_create_key"
        ),
        CheckConstraint(
            "status IN ('discovered', 'cancelled', 'expired')",
            name="ck_provisioning_status",
        ),
        CheckConstraint("revision >= 0", name="ck_provisioning_revision"),
        CheckConstraint("expires_at > created_at", name="ck_provisioning_expiry"),
        Index(
            "uq_provisioning_active_device",
            "device_id",
            unique=True,
            postgresql_where=text("status = 'discovered'"),
            sqlite_where=text("status = 'discovered'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    house_id: Mapped[str] = mapped_column(ForeignKey("houses.id", ondelete="RESTRICT"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="RESTRICT"))
    device_id: Mapped[str] = mapped_column(
        ForeignKey("physical_devices.device_id", ondelete="RESTRICT")
    )
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24), default="discovered")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    challenge_hash: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64))
    create_key_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    cancel_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    cancel_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
