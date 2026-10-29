from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DeviceCommandORM(Base):
    __tablename__ = "device_commands"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','dispatched','applied','failed','timed_out')",
            name="ck_command_status",
        ),
        CheckConstraint(
            "ack_status IS NULL OR ack_status IN ('accepted','applied','rejected','failed')",
            name="ck_command_ack",
        ),
        CheckConstraint("expires_at > created_at", name="ck_command_deadline"),
        UniqueConstraint(
            "house_id",
            "requested_by",
            "idempotency_hash",
            name="uq_command_idempotency",
        ),
        Index(
            "uq_command_pending_device",
            "device_id",
            unique=True,
            postgresql_where=text("status IN ('queued','dispatched')"),
            sqlite_where=text("status IN ('queued','dispatched')"),
        ),
        Index("ix_command_house_created", "house_id", "created_at"),
    )
    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(36), unique=True)
    house_id: Mapped[str] = mapped_column(ForeignKey("houses.id", ondelete="RESTRICT"))
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT")
    )
    requested_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    idempotency_hash: Mapped[str] = mapped_column(String(64))
    requested_on: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16))
    ack_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reported_on: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ack_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
