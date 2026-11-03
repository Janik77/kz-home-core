"""Private immutable control candidate and durable one-per-command grant binding."""

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PROFILE = "deadline-permit-v1"


class ControlCommandORM(Base):
    __tablename__ = "command_control"
    __table_args__ = (
        UniqueConstraint("permit_binding_sha256", name="uq_control_permit_binding"),
        CheckConstraint("length(authority_epoch) = 36", name="ck_control_epoch"),
        CheckConstraint(
            "execution_profile = 'deadline-permit-v1'", name="ck_control_profile"
        ),
        CheckConstraint("length(wire_sha256) = 64", name="ck_control_digest"),
        CheckConstraint("length(wire_payload) <= 4096", name="ck_control_payload"),
        CheckConstraint(
            "permit_binding_sha256 IS NULL OR length(permit_binding_sha256) = 64",
            name="ck_control_binding",
        ),
    )
    command_id: Mapped[str] = mapped_column(
        ForeignKey("device_commands.command_id", ondelete="RESTRICT"), primary_key=True
    )
    execution_profile: Mapped[str] = mapped_column(String(32))
    authority_epoch: Mapped[str] = mapped_column(String(36))
    wire_payload: Mapped[str] = mapped_column(Text)
    wire_sha256: Mapped[str] = mapped_column(String(64))
    permit_binding_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
