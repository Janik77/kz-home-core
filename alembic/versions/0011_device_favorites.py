"""Personal house-scoped device preferences; no device state or control changes."""

from alembic import op
import sqlalchemy as sa

revision = "0011_device_favorites"
down_revision = "0010_scene_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_favorites",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("house_id", sa.String(64), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(64),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "house_id"],
            ["house_memberships.user_id", "house_memberships.house_id"],
            name="fk_favorite_membership",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_favorite_device", "device_favorites", ["device_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM device_favorites")).scalar():
        raise RuntimeError("Device favorites are not empty; refusing preference loss")
    op.drop_index("ix_favorite_device", table_name="device_favorites")
    op.drop_table("device_favorites")
