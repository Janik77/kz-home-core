"""Revision counters for atomic scene and automation definition edits."""

from alembic import op
import sqlalchemy as sa

revision = "0009_definition_revisions"
down_revision = "0008_provisioning_sessions"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("scenes", "automations"):
        op.add_column(
            table,
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade():
    for table in ("automations", "scenes"):
        op.drop_column(table, "revision")
