"""Add persisted numeric freshness and per-rule threshold latch; no legacy conversion."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_numeric_thresholds"
down_revision = "0013_device_automations"
branch_labels = None
depends_on = None


def upgrade():
    for table, name in [
        ("devices", "numeric_observed_at"),
        ("devices", "numeric_watermarks"),
        ("automations", "threshold_state"),
    ]:
        op.add_column(
            table,
            sa.Column(
                name,
                sa.JSON().with_variant(JSONB(), "postgresql"),
                nullable=False,
                server_default="{}",
            ),
        )


def downgrade():
    # A downgrade could forget consumed edges and replay them on a later upgrade.
    rows = (
        op.get_bind()
        .execute(sa.text("SELECT threshold_state FROM automations"))
        .scalars()
    )
    if any(str(value) not in ("{}", "null", "None") for value in rows):
        raise RuntimeError("Threshold state exists; restore a verified backup instead")
    op.drop_column("automations", "threshold_state")
    op.drop_column("devices", "numeric_watermarks")
    op.drop_column("devices", "numeric_observed_at")
