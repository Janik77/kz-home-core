"""Durable provisioning attempts; no device claim or broker access."""

from alembic import context, op
import sqlalchemy as sa

revision = "0008_provisioning_sessions"
down_revision = "0007_control_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provisioning_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "room_id",
            sa.String(64),
            sa.ForeignKey("rooms.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.String(64),
            sa.ForeignKey("physical_devices.device_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("challenge_hash", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("create_key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("cancel_key_hash", sa.String(64), nullable=True),
        sa.Column(
            "cancel_actor_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("cancel_revision", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "house_id", "actor_id", "create_key_hash", name="uq_provisioning_create_key"
        ),
        sa.CheckConstraint(
            "status IN ('discovered', 'cancelled', 'expired')",
            name="ck_provisioning_status",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_provisioning_revision"),
        sa.CheckConstraint("expires_at > created_at", name="ck_provisioning_expiry"),
    )
    op.create_index(
        "uq_provisioning_active_device",
        "provisioning_sessions",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("status = 'discovered'"),
        sqlite_where=sa.text("status = 'discovered'"),
    )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Provisioning downgrade requires an online data check")
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM provisioning_sessions")):
        raise RuntimeError("Cannot downgrade: provisioning history is not empty")
    op.drop_index("uq_provisioning_active_device", table_name="provisioning_sessions")
    op.drop_table("provisioning_sessions")
