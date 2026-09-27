"""Support Device Protocol v1 correlation IDs without truncation."""

from alembic import context, op
import sqlalchemy as sa

revision = "0004_event_log_correlation"
down_revision = "0003_auth_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("event_logs") as batch:
        batch.alter_column(
            "correlation_id",
            existing_type=sa.String(36),
            type_=sa.String(128),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Never silently truncate IDs when returning to the old schema.
    if context.is_offline_mode():
        raise RuntimeError("Correlation ID downgrade requires an online data check")
    oversized = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM event_logs WHERE length(correlation_id) > 36")
    )
    if oversized:
        raise RuntimeError("Cannot downgrade: correlation IDs longer than 36 exist")
    with op.batch_alter_table("event_logs") as batch:
        batch.alter_column(
            "correlation_id",
            existing_type=sa.String(128),
            type_=sa.String(36),
            existing_nullable=False,
        )
