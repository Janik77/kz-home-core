"""Live database-time admission; never use transaction-start CURRENT_TIMESTAMP."""

from sqlalchemy import func, select, or_, and_, literal
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


def require_deadline(session, run_id, now, position=None):
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
    if run.source_kind == "scene":
        from app.services.scene_run_service import SceneRunService
        SceneRunService(session).require_not_draft(run.scene_id)
    if run.execution_mode == "server":
        # Recheck at command commit and again immediately before MQTT publish.
        from app.services.scene_run_service import SceneRunService

        if run.source_kind == "automation":
            from app.services.automation_runs import AutomationRunService

            service = AutomationRunService(session)
            service.before_dispatch(run)
        else:
            service = SceneRunService(session)
            service.require_authority(run.house_id, run.requested_by)
            service.before_dispatch(run)
    if position is not None:
        from app.models import SceneRunActionORM
        from app.services.command_service import utc

        action = session.get(
            SceneRunActionORM, (run_id, position), populate_existing=True
        )
        if action is None:
            raise ConflictError("Only relay actions can be dispatched")
        if action.dispatch_before is not None:
            bound = utc(action.dispatch_before)
            predicate = (
                literal(bound) > func.clock_timestamp()
                if session.get_bind().dialect.name == "postgresql"
                else func.julianday(bound) > func.julianday("now") + 1 / 86400000
            )
            if now >= bound or not session.scalar(select(predicate)):
                raise ConflictError("Action dispatch window has elapsed")
            deadline = min(utc(deadline), bound)
    return deadline
