"""Live database-time admission; never use transaction-start CURRENT_TIMESTAMP."""

from sqlalchemy import func, select, or_, and_
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


def lease_predicate(session):
    if session.get_bind().dialect.name == "postgresql":
        live = SceneRunORM.lease_until > func.clock_timestamp()
    else:
        live = (
            func.julianday(SceneRunORM.lease_until)
            > func.julianday("now") + 1 / 86400000
        )
    return or_(
        SceneRunORM.execution_mode == "manual",
        and_(
            live,
            SceneRunORM.execution_finished.is_(False),
            SceneRunORM.execution_error.is_(None),
        ),
    )


def require_deadline(session, run_id, now):
    deadline = session.scalar(
        select(SceneRunORM.dispatch_deadline).where(
            SceneRunORM.id == run_id,
            SceneRunORM.dispatch_deadline > now,
            deadline_predicate(session),
            lease_predicate(session),
        )
    )
    if deadline is None:
        raise ConflictError("Scene dispatch deadline has elapsed")
    run = session.get(SceneRunORM, run_id, populate_existing=True)
    if run.execution_mode == "server":
        # Recheck at command commit and again immediately before MQTT publish.
        from app.services.scene_run_service import SceneRunService

        SceneRunService(session).require_authority(run.house_id, run.requested_by)
    return deadline
