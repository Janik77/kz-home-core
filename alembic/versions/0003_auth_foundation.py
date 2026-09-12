"""Add user authentication, house membership, and refresh sessions."""

from alembic import op
import sqlalchemy as sa

revision = "0003_auth_foundation"
down_revision = "0002_automation_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
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
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "house_memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "house_id",
            sa.String(64),
            sa.ForeignKey("houses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
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
        sa.CheckConstraint(
            "role IN ('owner', 'installer', 'technician', 'resident')",
            name="ck_membership_role",
        ),
        sa.UniqueConstraint("user_id", "house_id", name="uq_membership_user_house"),
    )
    op.create_index("ix_house_memberships_user_id", "house_memberships", ["user_id"])
    op.create_index("ix_house_memberships_house_id", "house_memberships", ["house_id"])
    op.create_table(
        "refresh_sessions",
        sa.Column("jti_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])
    op.create_index(
        "ix_refresh_sessions_expires_at", "refresh_sessions", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("refresh_sessions")
    op.drop_table("house_memberships")
    op.drop_table("users")
