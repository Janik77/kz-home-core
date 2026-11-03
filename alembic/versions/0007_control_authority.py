"""Persist exact control candidates and single-use permit bindings; preserve ledger."""

from alembic import context, op
import sqlalchemy as sa

revision = "0007_control_authority"
down_revision = "0006_command_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "command_control",
        sa.Column(
            "command_id",
            sa.String(36),
            sa.ForeignKey("device_commands.command_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("execution_profile", sa.String(32), nullable=False),
        sa.Column("authority_epoch", sa.String(36), nullable=False),
        sa.Column("wire_payload", sa.Text(), nullable=False),
        sa.Column("wire_sha256", sa.String(64), nullable=False),
        sa.Column("permit_binding_sha256", sa.String(64), nullable=True),
        sa.UniqueConstraint("permit_binding_sha256", name="uq_control_permit_binding"),
        sa.CheckConstraint("length(authority_epoch) = 36", name="ck_control_epoch"),
        sa.CheckConstraint(
            "execution_profile = 'deadline-permit-v1'", name="ck_control_profile"
        ),
        sa.CheckConstraint("length(wire_sha256) = 64", name="ck_control_digest"),
        sa.CheckConstraint("length(wire_payload) <= 4096", name="ck_control_payload"),
        sa.CheckConstraint(
            "permit_binding_sha256 IS NULL OR length(permit_binding_sha256) = 64",
            name="ck_control_binding",
        ),
    )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Control downgrade requires an online data check")
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM command_control")):
        raise RuntimeError("Cannot downgrade: control history is not empty")
    op.drop_table("command_control")
