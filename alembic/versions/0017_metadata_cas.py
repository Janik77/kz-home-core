"""Add independent monotonic metadata revisions; telemetry does not advance them."""

from alembic import op
import sqlalchemy as sa

revision = "0017_metadata_cas"
down_revision = "0016_durable_waits"
branch_labels = depends_on = None
TABLES = ("houses", "floors", "rooms", "devices")


def upgrade():
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "metadata_revision", sa.Integer(), nullable=False, server_default="1"
            ),
        )


def downgrade():
    connection = op.get_bind()
    for table in TABLES:
        if connection.scalar(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE metadata_revision != 1")
        ):
            raise RuntimeError(
                "Metadata revisions have been used; restore an older backup"
            )
    for table in reversed(TABLES):
        op.drop_column(table, "metadata_revision")
