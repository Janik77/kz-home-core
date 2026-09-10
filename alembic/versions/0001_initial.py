"""Initial KZ Home schema."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def timestamps():
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.create_table(
        "houses",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        *timestamps(),
    )
    op.create_table(
        "floors",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_floors_house_id", "floors", ["house_id"])
    op.create_table(
        "rooms",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "floor_id",
            sa.String(64),
            sa.ForeignKey("floors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("icon", sa.String(100)),
        *timestamps(),
    )
    op.create_index("ix_rooms_floor_id", "rooms", ["floor_id"])
    op.create_table(
        "devices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "room_id",
            sa.String(64),
            sa.ForeignKey("rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("state", json_type, nullable=False),
        sa.Column("capabilities", json_type, nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("online", sa.Boolean(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_devices_room_id", "devices", ["room_id"])
    op.create_index("ix_devices_type", "devices", ["type"])
    op.create_table(
        "scenes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actions", json_type, nullable=False),
        *timestamps(),
    )
    op.create_index("ix_scenes_house_id", "scenes", ["house_id"])
    op.create_table(
        "automations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("trigger", json_type, nullable=False),
        sa.Column("conditions", json_type, nullable=False),
        sa.Column("actions", json_type, nullable=False),
        *timestamps(),
    )
    op.create_index("ix_automations_house_id", "automations", ["house_id"])


def downgrade() -> None:
    op.drop_table("automations")
    op.drop_table("scenes")
    op.drop_table("devices")
    op.drop_table("rooms")
    op.drop_table("floors")
    op.drop_table("houses")
