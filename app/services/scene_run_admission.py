"""Live database-time admission; never use transaction-start CURRENT_TIMESTAMP."""

from sqlalchemy import func, select
from app.core.errors import ConflictError
from app.models import SceneRunORM


def deadline_predicate(session):
    if session.get_bind().dialect.name == "postgresql":
        return SceneRunORM.dispatch_deadline > func.clock_timestamp()
    # SQLite's clock is millisecond resolution. Reject the boundary millisecond
    # conservatively rather than granting a fractional extension.
    return (
        func.julianday(SceneRunORM.dispatch_deadline)
        > func.julianday("now") + 1 / 86400000
    )


def require_deadline(session, run_id, now):
    deadline = session.scalar(
        select(SceneRunORM.dispatch_deadline).where(
            SceneRunORM.id == run_id,
            SceneRunORM.dispatch_deadline > now,
            deadline_predicate(session),
        )
    )
    if deadline is None:
        raise ConflictError("Scene dispatch deadline has elapsed")
    return deadline
