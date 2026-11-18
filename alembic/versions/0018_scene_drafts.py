"""Copied scenes remain non-executable until explicit activation."""

from alembic import op
import sqlalchemy as sa

revision = "0018_scene_drafts"
down_revision = "0017_metadata_cas"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "scenes",
        sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM scenes WHERE is_draft = true")
    ):
        raise RuntimeError(
            "Scene drafts exist; downgrade would activate them; restore an older backup"
        )
    op.drop_column("scenes", "is_draft")
