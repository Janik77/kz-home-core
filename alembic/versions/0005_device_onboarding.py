"""Add trusted physical inventory and permanent house binding."""

from alembic import context, op
import sqlalchemy as sa

revision = "0005_device_onboarding"
down_revision = "0004_event_log_correlation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "physical_devices",
        sa.Column("device_id", sa.String(64), primary_key=True),
        sa.Column("hardware_id", sa.String(128), nullable=False, unique=True),
        sa.Column("hardware_model", sa.String(64), nullable=False),
        sa.Column("protocol_version", sa.String(2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("claim_code_hash", sa.String(64), nullable=True),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "bound_device_id",
            sa.String(64),
            sa.ForeignKey("devices.id", ondelete="RESTRICT"),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('unprovisioned', 'provisioning', 'active', 'inactive', 'revoked')",
            name="ck_physical_status",
        ),
        sa.CheckConstraint("protocol_version = 'v1'", name="ck_physical_protocol"),
        sa.CheckConstraint(
            "(status = 'unprovisioned' AND house_id IS NULL AND bound_device_id IS NULL "
            "AND claim_code_hash IS NOT NULL) OR "
            "(status <> 'unprovisioned' AND house_id IS NOT NULL "
            "AND bound_device_id IS NOT NULL AND bound_device_id = device_id AND claim_code_hash IS NULL)",
            name="ck_physical_binding",
        ),
    )
    op.create_index("ix_physical_devices_house_id", "physical_devices", ["house_id"])


def downgrade() -> None:
    # Dropping populated inventory would erase revocation and allow ID reuse.
    if context.is_offline_mode():
        raise RuntimeError("Onboarding downgrade requires an online data check")
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM physical_devices")):
        raise RuntimeError("Cannot downgrade: physical inventory is not empty")
    op.drop_table("physical_devices")
