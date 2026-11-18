"""Checkpoint/resume only explicit waits. Never recover a claimed command."""

from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update
from app.models import (
    HouseORM,
    SceneRunORM,
    SceneRunActionORM,
    SceneRunWaitORM,
    SceneRunReceiptORM,
    DeviceCommandORM,
)
from app.core.errors import ConflictError, EntityNotFoundError, InvalidReferenceError
from app.schemas.scene_runs import SceneRunActionRead
from app.services.command_service import utc
from app.services.scene_run_admission import deadline_predicate, lease_predicate

GRACE = timedelta(seconds=30)


def wait_action_view(run, action, predecessor_ok, expired, interrupted, now):
    due = utc(action.due_at) if action.due_at else None
    if action.completed_at is not None:
        status = "completed"
    elif (
        expired
        or run.execution_error in {"wait_expired", "wait_clock_reversed"}
        or (due and now >= due + GRACE)
    ):
        status = "expired"
    elif (
        run.cancelled_at or run.execution_error or run.execution_finished or interrupted
    ):
        status = "cancelled"
    elif action.started_at is not None:
        status = "waiting"
    else:
        status = "ready" if predecessor_ok else "waiting"
    return SceneRunActionRead(
        position=action.position,
        type="durable_wait",
        device_id=None,
        requested_on=None,
        seconds=action.seconds,
        due_at=due,
        resume_deadline=due + GRACE if due else None,
        status=status,
        failure_code=run.execution_error
        if status in {"cancelled", "expired"}
        else None,
    )


def locked_run(service, house_id, run_id):
    service.session.scalar(
        select(HouseORM).where(HouseORM.id == house_id).with_for_update()
    )
    row = service.session.scalar(
        select(SceneRunORM)
        .where(
            SceneRunORM.id == run_id,
            SceneRunORM.house_id == house_id,
            SceneRunORM.source_kind == service.source_kind,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise EntityNotFoundError("Resource not found")
    return row


def begin_wait(service, house_id, run_id, position, token):
    db = service.session
    row = locked_run(service, house_id, run_id)
    service.before_dispatch(row)
    if (
        not row.durable_plan
        or row.executor_token != token
        or row.wait_position is not None
    ):
        raise ConflictError("Wait is not owned by this executor")
    action = db.get(SceneRunWaitORM, (run_id, position), populate_existing=True)
    link = db.get(SceneRunReceiptORM, (run_id, position - 1))
    previous = (
        db.get(DeviceCommandORM, link.command_id, populate_existing=True)
        if link
        else None
    )
    successor = db.get(
        SceneRunActionORM, (run_id, position + 1), populate_existing=True
    )
    now = service.now()
    if (
        action is None
        or action.started_at is not None
        or previous is None
        or previous.status != "applied"
        or previous.ack_status != "applied"
        or previous.reported_on != previous.requested_on
        or previous.completed_at is None
        or successor is None
        or successor.claimed
        or db.get(SceneRunReceiptORM, (run_id, position + 1)) is not None
    ):
        raise ConflictError(
            "Wait requires confirmed predecessor and never-sent successor"
        )
    started = utc(previous.completed_at)
    due = started + timedelta(seconds=action.seconds)
    if started > now or due >= utc(row.dispatch_deadline) or now >= due + GRACE:
        raise ConflictError("Wait timing is no longer valid")
    claimed = db.execute(
        update(SceneRunORM)
        .where(
            SceneRunORM.id == run_id,
            SceneRunORM.executor_token == token,
            SceneRunORM.wait_position.is_(None),
            SceneRunORM.execution_finished.is_(False),
            SceneRunORM.execution_error.is_(None),
            SceneRunORM.lease_until > now,
            deadline_predicate(db),
            lease_predicate(db),
        )
        .values(
            wait_position=position,
            wait_checked_at=now,
            executor_token=None,
            lease_until=None,
        )
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise ConflictError("Wait checkpoint ownership lost")
    action.started_at, action.due_at = started, due
    db.commit()  # No continuation can precede this durable checkpoint.


def finish_wait(row, now, reason):
    row.execution_finished = True
    row.execution_error = reason
    row.executor_token = None
    row.lease_until = None
    if reason not in {"wait_expired", "wait_clock_reversed"}:
        row.cancelled_at = now


def cancel_wait(service, house_id, run_id, user_id, *, creator_only=True):
    row = locked_run(service, house_id, run_id)
    if creator_only and row.requested_by != user_id:
        raise EntityNotFoundError("Resource not found")
    if not row.durable_plan:
        raise ConflictError("Only durable waiting plans can be cancelled")
    if row.cancelled_at is not None:
        return service.get(house_id, run_id)
    if (
        row.wait_position is None
        or row.execution_finished
        or row.execution_error
        or row.executor_token is not None
    ):
        raise ConflictError("Run is not waiting; accepted commands cannot be recalled")
    wait = service.session.get(SceneRunWaitORM, (run_id, row.wait_position))
    now = service.now()
    if (
        wait is None
        or wait.due_at is None
        or now >= utc(wait.due_at) + GRACE
        or now >= utc(row.dispatch_deadline)
    ):
        raise ConflictError("Run wait has expired")
    finish_wait(row, now, "cancelled")
    service.session.commit()
    return service.get(house_id, run_id)


async def resume_waits(session_factory, transport, executor, *, now):
    with session_factory() as db:
        candidates = list(
            db.execute(
                select(SceneRunORM.id, SceneRunORM.house_id, SceneRunORM.source_kind)
                .join(
                    SceneRunWaitORM,
                    (SceneRunWaitORM.run_id == SceneRunORM.id)
                    & (SceneRunWaitORM.position == SceneRunORM.wait_position),
                )
                .where(
                    SceneRunORM.durable_plan.is_(True),
                    SceneRunORM.wait_position.is_not(None),
                    SceneRunORM.execution_finished.is_(False),
                )
                .order_by(SceneRunWaitORM.due_at)
                .limit(128)
            )
        )
    for run_id, house_id, kind in candidates:
        with session_factory() as db:
            from app.services.scene_run_service import SceneRunService
            from app.services.automation_runs import AutomationRunService

            service = (
                AutomationRunService if kind == "automation" else SceneRunService
            )(db, transport, now=now)
            row = locked_run(service, house_id, run_id)
            if (
                row.execution_finished
                or row.execution_error
                or row.wait_position is None
                or row.executor_token is not None
            ):
                continue
            point = row.wait_position
            wait = db.get(SceneRunWaitORM, (run_id, point), populate_existing=True)
            successor = db.get(
                SceneRunActionORM, (run_id, point + 1), populate_existing=True
            )
            current = now()
            if (
                wait is None
                or wait.due_at is None
                or wait.started_at is None
                or wait.completed_at is not None
                or successor is None
                or successor.claimed
                or db.get(SceneRunReceiptORM, (run_id, point + 1)) is not None
            ):
                finish_wait(row, current, "wait_checkpoint_invalid")
                db.commit()
                continue
            if current < utc(wait.started_at) or (
                row.wait_checked_at and current < utc(row.wait_checked_at)
            ):
                finish_wait(row, current, "wait_clock_reversed")
                db.commit()
                continue
            if current >= utc(wait.due_at) + GRACE or current >= utc(
                row.dispatch_deadline
            ):
                finish_wait(row, current, "wait_expired")
                db.commit()
                continue
            try:
                service.before_dispatch(row)
            except (ConflictError, EntityNotFoundError, InvalidReferenceError):
                finish_wait(row, current, "continuation_invalid")
                db.commit()
                continue
            row.wait_checked_at = current
            if current < utc(wait.due_at):
                db.commit()
                continue
            token = str(uuid4())
            claim = db.execute(
                update(SceneRunORM)
                .where(
                    SceneRunORM.id == run_id,
                    SceneRunORM.wait_position == point,
                    SceneRunORM.executor_token.is_(None),
                    SceneRunORM.execution_finished.is_(False),
                    SceneRunORM.execution_error.is_(None),
                    deadline_predicate(db),
                )
                .values(
                    wait_position=None,
                    executor_token=token,
                    lease_until=current + GRACE,
                )
            )
            if claim.rowcount != 1:
                db.rollback()
                continue
            wait.completed_at = current
            successor.dispatch_before = utc(wait.due_at) + GRACE
            db.commit()  # Crash after claim loses continuation; startup never reclaims it.
            executor.start(house_id, run_id, token)
