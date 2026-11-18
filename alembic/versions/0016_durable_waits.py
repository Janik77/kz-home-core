"""Add explicit wait checkpoints; existing relay actions/receipts remain untouched."""

import json
from alembic import op
import sqlalchemy as sa

revision = "0016_durable_waits"
down_revision = "0015_house_schedules"
branch_labels = depends_on = None


def upgrade():
    for column in [
        sa.Column(
            "durable_plan", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("wait_position", sa.Integer(), nullable=True),
        sa.Column("wait_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    ]:
        op.add_column("scene_runs", column)
    op.add_column(
        "scene_run_actions",
        sa.Column("dispatch_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "scene_run_waits",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("scene_runs.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("position > 0", name="ck_wait_position"),
        sa.CheckConstraint("seconds >= 1 AND seconds <= 3600", name="ck_wait_seconds"),
    )


def downgrade():
    connection = op.get_bind()
    for table in ("scenes", "automations"):
        for actions in connection.execute(
            sa.text(f"SELECT actions FROM {table}")
        ).scalars():
            actions = json.loads(actions) if isinstance(actions, str) else actions
            if any(a.get("type") == "durable_wait" for a in actions):
                raise RuntimeError(
                    "Durable wait definitions exist; restore a verified backup"
                )
    if (
        connection.execute(
            sa.text(
                "SELECT 1 FROM scene_runs WHERE durable_plan OR wait_position IS NOT NULL OR wait_checked_at IS NOT NULL OR cancelled_at IS NOT NULL LIMIT 1"
            )
        ).first()
        or connection.execute(sa.text("SELECT 1 FROM scene_run_waits LIMIT 1")).first()
        or connection.execute(
            sa.text(
                "SELECT 1 FROM scene_run_actions WHERE dispatch_before IS NOT NULL LIMIT 1"
            )
        ).first()
    ):
        raise RuntimeError("Durable wait history exists; restore a verified backup")
    op.drop_table("scene_run_waits")
    op.drop_column("scene_run_actions", "dispatch_before")
    for name in ("cancelled_at", "wait_checked_at", "wait_position", "durable_plan"):
        op.drop_column("scene_runs", name)
