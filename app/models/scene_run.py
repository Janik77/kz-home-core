"""Immutable intent and at-most-once dispatch claims; receipts remain authoritative."""

from datetime import datetime
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class SceneRunORM(Base):
    __tablename__ = "scene_runs"
    __table_args__ = (
        UniqueConstraint(
            "house_id", "requested_by", "idempotency_hash", name="uq_scene_run_key"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    house_id: Mapped[str] = mapped_column(ForeignKey("houses.id", ondelete="RESTRICT"))
    requested_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    idempotency_hash: Mapped[str] = mapped_column(String(64))
    # Keep the snapshot even if its definition is subsequently deleted.
    scene_id: Mapped[str] = mapped_column(String(64))
    scene_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatch_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    execution_mode: Mapped[str] = mapped_column(
        String(16), default="manual", server_default="manual"
    )
    executor_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_finished: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )


class SceneRunActionORM(Base):
    __tablename__ = "scene_run_actions"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_scene_action_position"),
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("scene_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64))
    requested_on: Mapped[bool] = mapped_column(Boolean)
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SceneRunReceiptORM(Base):
    __tablename__ = "scene_run_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "position"],
            ["scene_run_actions.run_id", "scene_run_actions.position"],
            ondelete="RESTRICT",
        ),
    )
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("device_commands.command_id", ondelete="RESTRICT"), unique=True
    )
