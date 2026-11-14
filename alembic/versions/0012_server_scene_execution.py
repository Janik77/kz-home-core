"""At-most-once server scene execution ownership; legacy runs stay manual."""

from alembic import op
import sqlalchemy as sa

revision = "0012_server_scene_execution"
down_revision = "0011_device_favorites"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "scene_runs",
        sa.Column(
            "execution_mode", sa.String(16), nullable=False, server_default="manual"
        ),
    )
    op.add_column(
        "scene_runs", sa.Column("executor_token", sa.String(36), nullable=True)
    )
    op.add_column(
        "scene_runs",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scene_runs", sa.Column("execution_error", sa.String(32), nullable=True)
    )
    op.add_column(
        "scene_runs",
        sa.Column(
            "execution_finished",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM scene_runs WHERE execution_mode = 'server'")
        )
        .scalar()
    ):
        raise RuntimeError("Server scene history exists; refusing evidence loss")
    for column in (
        "execution_finished",
        "execution_error",
        "lease_until",
        "executor_token",
        "execution_mode",
    ):
        op.drop_column("scene_runs", column)
