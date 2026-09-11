"""Add automation engine event history and lookup index."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_automation_engine"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_index(
        "ix_automations_house_enabled",
        "automations",
        ["house_id", "enabled"],
    )
    op.create_table(
        "event_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_event_logs_house_created",
        "event_logs",
        ["house_id", "created_at"],
    )
    op.create_index("ix_event_logs_type", "event_logs", ["event_type"])
    op.create_index("ix_event_logs_correlation_id", "event_logs", ["correlation_id"])


def downgrade() -> None:
    op.drop_table("event_logs")
    op.drop_index("ix_automations_house_enabled", table_name="automations")
