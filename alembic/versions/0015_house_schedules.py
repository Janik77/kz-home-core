"""Explicit house-zone schedules; no conversion or replay of historical rules."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_house_schedules"
down_revision = "0014_numeric_thresholds"
branch_labels = depends_on = None


def upgrade():
    for table, column in [
        ("houses", sa.Column("timezone", sa.String(128), nullable=True)),
        (
            "houses",
            sa.Column(
                "timezone_revision", sa.Integer(), nullable=False, server_default="1"
            ),
        ),
        (
            "houses",
            sa.Column(
                "timezone_effective_after", sa.DateTime(timezone=True), nullable=True
            ),
        ),
        (
            "houses",
            sa.Column("schedule_cursor", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "automations",
            sa.Column(
                "schedule_effective_after", sa.DateTime(timezone=True), nullable=True
            ),
        ),
        (
            "automations",
            sa.Column(
                "schedule_state",
                sa.JSON().with_variant(JSONB(), "postgresql"),
                nullable=False,
                server_default="{}",
            ),
        ),
        (
            "scene_runs",
            sa.Column("schedule_timezone_revision", sa.Integer(), nullable=True),
        ),
    ]:
        op.add_column(table, column)


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT 1 FROM houses WHERE timezone IS NOT NULL OR schedule_cursor IS NOT NULL OR timezone_revision > 1 OR timezone_effective_after IS NOT NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError("Schedule data exists; restore a verified backup instead")
    rows = connection.execute(
        sa.text("SELECT schedule_state, trigger FROM automations")
    ).all()
    import json

    for state, trigger in rows:
        state = json.loads(state) if isinstance(state, str) else state
        trigger = json.loads(trigger) if isinstance(trigger, str) else trigger
        if state or trigger.get("type") == "schedule":
            raise RuntimeError(
                "Schedule definitions/checkpoints exist; restore a verified backup instead"
            )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM scene_runs WHERE schedule_timezone_revision IS NOT NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError("Schedule history exists; restore a verified backup instead")
    for table, name in [
        ("scene_runs", "schedule_timezone_revision"),
        ("automations", "schedule_state"),
        ("automations", "schedule_effective_after"),
        ("houses", "schedule_cursor"),
        ("houses", "timezone_effective_after"),
        ("houses", "timezone_revision"),
        ("houses", "timezone"),
    ]:
        op.drop_column(table, name)
