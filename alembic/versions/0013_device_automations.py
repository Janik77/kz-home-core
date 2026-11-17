"""Durable automation source and explicit authority; existing rules fail closed."""

from alembic import op
import sqlalchemy as sa

revision = "0013_device_automations"
down_revision = "0012_server_scene_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "devices",
        sa.Column("state_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "automations", sa.Column("authorized_by", sa.String(64), nullable=True)
    )
    op.add_column(
        "scene_runs",
        sa.Column("source_kind", sa.String(16), nullable=False, server_default="scene"),
    )
    op.create_index(
        "uq_automation_event",
        "scene_runs",
        ["source_kind", "scene_id", "idempotency_hash"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'automation'"),
        sqlite_where=sa.text("source_kind = 'automation'"),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM scene_runs WHERE source_kind = 'automation'")
        )
        .scalar()
    ):
        raise RuntimeError("Automation history exists; refusing evidence loss")
    op.drop_index("uq_automation_event", table_name="scene_runs")
    op.drop_column("scene_runs", "source_kind")
    op.drop_column("automations", "authorized_by")
    op.drop_column("devices", "state_observed_at")
