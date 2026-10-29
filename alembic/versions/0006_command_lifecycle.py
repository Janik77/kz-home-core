"""Persist bounded relay command attempts and correlated protocol evidence."""

from alembic import context, op
import sqlalchemy as sa

revision = "0006_command_lifecycle"
down_revision = "0005_device_onboarding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_commands",
        sa.Column("command_id", sa.String(36), primary_key=True),
        sa.Column("correlation_id", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.String(64),
            sa.ForeignKey("devices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("requested_on", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ack_status", sa.String(16), nullable=True),
        sa.Column("reported_on", sa.Boolean(), nullable=True),
        sa.Column("failure_code", sa.String(32), nullable=True),
        *[
            sa.Column(name, sa.DateTime(timezone=True), nullable=False)
            for name in ("created_at", "updated_at", "expires_at")
        ],
        *[
            sa.Column(name, sa.DateTime(timezone=True), nullable=True)
            for name in (
                "dispatch_started_at",
                "dispatched_at",
                "ack_received_at",
                "state_received_at",
                "completed_at",
            )
        ],
        sa.CheckConstraint(
            "status IN ('queued','dispatched','applied','failed','timed_out')",
            name="ck_command_status",
        ),
        sa.CheckConstraint(
            "ack_status IS NULL OR ack_status IN ('accepted','applied','rejected','failed')",
            name="ck_command_ack",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_command_deadline"),
        sa.UniqueConstraint(
            "house_id",
            "requested_by",
            "idempotency_hash",
            name="uq_command_idempotency",
        ),
    )
    op.create_index(
        "uq_command_pending_device",
        "device_commands",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','dispatched')"),
        sqlite_where=sa.text("status IN ('queued','dispatched')"),
    )
    op.create_index(
        "ix_command_house_created", "device_commands", ["house_id", "created_at"]
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Command downgrade requires an online data check")
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM device_commands")):
        raise RuntimeError("Cannot downgrade: command history is not empty")
    op.drop_table("device_commands")
