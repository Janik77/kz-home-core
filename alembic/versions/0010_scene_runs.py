"""Durable software scene intent and dispatch claims, without a replay worker."""

from alembic import op
import sqlalchemy as sa

revision = "0010_scene_runs"
down_revision = "0009_definition_revisions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scene_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=False),
        sa.Column("scene_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatch_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "house_id", "requested_by", "idempotency_hash", name="uq_scene_run_key"
        ),
    )
    op.create_table(
        "scene_run_actions",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("scene_runs.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("requested_on", sa.Boolean(), nullable=False),
        sa.Column("claimed", sa.Boolean(), nullable=False),
        sa.Column("failure_code", sa.String(32), nullable=True),
        sa.CheckConstraint("position >= 0", name="ck_scene_action_position"),
    )

    op.create_table(
        "scene_run_receipts",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column(
            "command_id",
            sa.String(36),
            sa.ForeignKey("device_commands.command_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "position"],
            ["scene_run_actions.run_id", "scene_run_actions.position"],
            ondelete="RESTRICT",
        ),
    )


def downgrade():
    # Evidence cannot silently disappear during rollback.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM scene_runs")).scalar():
        raise RuntimeError("Cannot downgrade: scene run history is not empty")
    op.drop_table("scene_run_receipts")
    op.drop_table("scene_run_actions")
    op.drop_table("scene_runs")
