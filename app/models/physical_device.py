"""Trusted physical inventory, separate from human identities and public metadata."""

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.entities import TimestampMixin


class PhysicalDeviceORM(TimestampMixin, Base):
    __tablename__ = "physical_devices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('unprovisioned', 'provisioning', 'active', 'inactive', 'revoked')",
            name="ck_physical_status",
        ),
        CheckConstraint("protocol_version = 'v1'", name="ck_physical_protocol"),
        CheckConstraint(
            "(status = 'unprovisioned' AND house_id IS NULL AND bound_device_id IS NULL "
            "AND claim_code_hash IS NOT NULL) OR "
            "(status <> 'unprovisioned' AND house_id IS NOT NULL "
            "AND bound_device_id IS NOT NULL AND bound_device_id = device_id AND claim_code_hash IS NULL)",
            name="ck_physical_binding",
        ),
    )
    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hardware_id: Mapped[str] = mapped_column(String(128), unique=True)
    hardware_model: Mapped[str] = mapped_column(String(64))
    protocol_version: Mapped[str] = mapped_column(String(2), default="v1")
    status: Mapped[str] = mapped_column(String(20), default="unprovisioned")
    claim_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    house_id: Mapped[str | None] = mapped_column(
        ForeignKey("houses.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    bound_device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
