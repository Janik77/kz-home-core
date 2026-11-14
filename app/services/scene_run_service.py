"""Caller-driven, fail-closed software scene runs. Never automatically replay."""

from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from app.core.errors import (
    ConflictError,
    DefinitionRevisionConflict,
    EntityNotFoundError,
    InvalidReferenceError,
    UnavailableError,
)
from app.models import (
    SceneRunORM,
    SceneRunActionORM,
    DeviceCommandORM,
    SceneRunReceiptORM,
)
from app.repositories.scene_repository import SceneRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.repositories.command_repository import CommandRepository
from app.schemas.scene_runs import SceneRunRead, SceneRunActionRead
from app.services.command_service import CommandService, utc
from app.services.command_clock import command_clock
from app.services.onboarding_service import PhysicalBindingGuard
from app.services.scene_run_admission import deadline_predicate, lease_predicate


class SceneRunService:
    def __init__(self, session, transport=None, *, now=command_clock.now):
        self.session, self.transport, self.now = session, transport, now

    def require_authority(self, house_id, user_id):
        from app.models import UserORM
        from app.repositories.auth_repository import MembershipRepository
        from app.services.auth_service import AuthorizationService
        from app.security import Permission

        user = self.session.get(UserORM, user_id, populate_existing=True)
        if user is None or not user.is_active:
            raise ConflictError("Scene authorization revoked")
        self.session.expire_all()
        auth = AuthorizationService(MembershipRepository(self.session))
        membership = auth.get_house_membership(user_id, house_id)
        if not all(
            auth.has_permission(membership, permission)
            for permission in (Permission.SCENE_RUN, Permission.DEVICE_CONTROL)
        ):
            raise ConflictError("Scene authorization revoked")

    def by_key(self, house_id, user_id, key):
        row = self.session.scalar(
            select(SceneRunORM).filter_by(
                house_id=house_id,
                requested_by=user_id,
                idempotency_hash=sha256(key.encode("ascii")).hexdigest(),
            )
        )
        if row is None:
            raise EntityNotFoundError("Resource not found")
        return self.get(house_id, row.id)

    @staticmethod
    def action_key(run_id, position):
        return f"scene_{run_id}_{position}"

    def row(self, house_id, run_id):
        row = self.session.scalar(
            select(SceneRunORM).filter_by(id=run_id, house_id=house_id)
        )
        if row is None:
            raise EntityNotFoundError("Resource not found")
        return row

    def actions(self, run_id):
        return list(
            self.session.scalars(
                select(SceneRunActionORM)
                .filter_by(run_id=run_id)
                .order_by(SceneRunActionORM.position)
                .execution_options(populate_existing=True)
            )
        )

    async def create_server(self, house_id, user_id, key, data, token):
        previous = self.session.scalar(
            select(SceneRunORM).filter_by(
                house_id=house_id,
                requested_by=user_id,
                idempotency_hash=sha256(key.encode("ascii")).hexdigest(),
            )
        )
        if previous is not None:
            return self.replay(previous, data, token)
        scene = SceneRepository(self.session).get(data.scene_id)
        if scene.house_id != house_id:
            raise EntityNotFoundError("Resource not found")
        if scene.revision != data.expected_revision:
            raise DefinitionRevisionConflict()
        self.require_authority(house_id, user_id)
        if self.transport is None or not getattr(self.transport, "connected", False):
            raise UnavailableError("MQTT transport unavailable")
        for action in scene.actions:
            # Includes software allowlist; no command publication here.
            await self.transport.authorize_command(house_id, action["device_id"])
        return self.create(house_id, user_id, key, data, executor_token=token)

    def create(self, house_id, user_id, key, data, *, executor_token=None):
        if executor_token:
            from app.models import HouseORM, SceneORM

            self.session.expire_all()
            self.session.scalar(
                select(HouseORM).where(HouseORM.id == house_id).with_for_update()
            )
            self.session.scalar(
                select(SceneORM).where(SceneORM.id == data.scene_id).with_for_update()
            )
        digest = sha256(key.encode("ascii")).hexdigest()
        criteria = dict(
            house_id=house_id, requested_by=user_id, idempotency_hash=digest
        )
        previous = self.session.scalar(select(SceneRunORM).filter_by(**criteria))
        if previous is not None:
            return self.replay(previous, data, executor_token)
        scene = SceneRepository(self.session).get(data.scene_id)
        if scene.house_id != house_id:
            raise EntityNotFoundError("Resource not found")
        if scene.revision != data.expected_revision:
            raise DefinitionRevisionConflict()
        actions = scene.actions
        if not 1 <= len(actions) <= 16:
            raise InvalidReferenceError("Scene runs require 1 to 16 relay actions")
        devices = DeviceRepository(self.session)
        guard = PhysicalBindingGuard(PhysicalDeviceRepository(self.session))
        for action in actions:
            device = devices.get(action["device_id"])
            if devices.house_id(device.id) != house_id:
                raise InvalidReferenceError("Scene device belongs to another house")
            guard.require_active(device.id)
            guard.require_command_contract(device.id)
            state = action["state"]
            if (
                device.type != "relay"
                or "on_off" not in device.capabilities
                or set(state) != {"on"}
                or type(state["on"]) is not bool
            ):
                raise InvalidReferenceError(
                    "Scene runs support only software relay Boolean on/off"
                )
        if executor_token is not None:
            active = self.session.scalar(
                select(SceneRunORM.id)
                .where(
                    SceneRunORM.house_id == house_id,
                    SceneRunORM.scene_id == data.scene_id,
                    SceneRunORM.execution_mode == "server",
                    SceneRunORM.execution_finished.is_(False),
                    SceneRunORM.lease_until > self.now(),
                    lease_predicate(self.session),
                )
                .limit(1)
            )
            if active is not None:
                raise ConflictError("Scene already has an active server run")
            # All actions are checked before any dispatch; these checks are repeated
            # at admission because inventory/permissions may change afterwards.
            self.require_authority(house_id, user_id)
            if self.transport is None or not getattr(
                self.transport, "connected", False
            ):
                raise UnavailableError("MQTT transport is unavailable")
            for action in actions:
                device = devices.get(action["device_id"])
                if not device.online:
                    raise ConflictError("Scene contains an offline device")
                pending = CommandRepository(self.session).pending(device.id)
                if pending is not None:
                    CommandService(
                        CommandRepository(self.session), self.transport, now=self.now
                    ).expire(pending)
                    if pending.status in {"queued", "dispatched"}:
                        raise ConflictError("Scene device has a pending command")
        now = self.now()
        row = SceneRunORM(
            id=str(uuid4()),
            scene_id=scene.id,
            scene_revision=scene.revision,
            created_at=now,
            dispatch_deadline=now + timedelta(minutes=5),
            execution_mode="server" if executor_token else "manual",
            executor_token=executor_token,
            lease_until=now + timedelta(seconds=30) if executor_token else None,
            execution_finished=False,
            **criteria,
        )
        self.session.add(row)
        try:
            self.session.flush()
            self.session.add_all(
                [
                    SceneRunActionORM(
                        run_id=row.id,
                        position=i,
                        device_id=a["device_id"],
                        requested_on=a["state"]["on"],
                        claimed=False,
                    )
                    for i, a in enumerate(actions)
                ]
            )
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            previous = self.session.scalar(select(SceneRunORM).filter_by(**criteria))
            if previous is None:
                raise ConflictError("Scene run creation conflicted") from None
            return self.replay(previous, data, executor_token)
        return self.get(house_id, row.id)

    def replay(self, row, data, executor_token=None):
        if (
            row.execution_mode != ("server" if executor_token else "manual")
            or row.scene_id != data.scene_id
            or row.scene_revision != data.expected_revision
        ):
            raise ConflictError("Idempotency key was used for another scene intent")
        return self.get(row.house_id, row.id)

    def get(self, house_id, run_id):
        run = self.row(house_id, run_id)
        interrupted = (
            run.execution_mode == "server"
            and not run.execution_finished
            and (run.lease_until is None or utc(run.lease_until) <= self.now())
        )
        execution_error = run.execution_error or (
            "executor_lost" if interrupted else None
        )
        results = []
        predecessor_ok = True
        stopped = False
        expired = utc(run.dispatch_deadline) <= self.now() or not self.session.scalar(
            select(
                exists(
                    select(SceneRunORM.id).where(
                        SceneRunORM.id == run_id, deadline_predicate(self.session)
                    )
                )
            )
        )
        commands = CommandService(
            CommandRepository(self.session), self.transport, now=self.now
        )
        for action in self.actions(run_id):
            # The link is committed atomically with command intent before publish.
            link = self.session.get(SceneRunReceiptORM, (run_id, action.position))
            receipt = (
                self.session.get(DeviceCommandORM, link.command_id) if link else None
            )
            value = None
            failure = action.failure_code
            if receipt is not None:
                if (
                    receipt.device_id != action.device_id
                    or receipt.requested_on != action.requested_on
                ):
                    status, failure = "blocked", "receipt_intent_mismatch"
                else:
                    # This method acquires the existing command lock and re-reads evidence.
                    value = commands.get(house_id, receipt.command_id)
                    status, failure = value.status, value.failure_code
            elif failure is not None:
                status = "blocked"
            elif action.claimed:
                status = (
                    "waiting"
                    if run.execution_mode == "server"
                    and not interrupted
                    and not run.execution_finished
                    and not run.execution_error
                    else "dispatch_unknown"
                )
            elif stopped or expired or interrupted or run.execution_error:
                status = "skipped"
            else:
                status = "ready" if predecessor_ok else "waiting"
            results.append(
                SceneRunActionRead(
                    position=action.position,
                    device_id=action.device_id,
                    requested_on=action.requested_on,
                    status=status,
                    failure_code=failure,
                    receipt=value,
                )
            )
            predecessor_ok = predecessor_ok and status == "applied"
            stopped = stopped or status in {
                "blocked",
                "failed",
                "timed_out",
                "dispatch_unknown",
            }
        statuses = {a.status for a in results}
        if statuses == {"applied"}:
            status = "succeeded"
        elif "dispatch_unknown" in statuses:
            status = "unknown"
        elif statuses & {"queued", "dispatched"}:
            status = "running"
        elif statuses & {"blocked", "failed", "timed_out"}:
            status = "stopped"
        elif expired:
            status = "expired"
        else:
            status = "ready"
        if run.execution_mode == "server" and status != "succeeded":
            uncertain = any(
                a.status in {"dispatch_unknown", "timed_out"}
                or a.failure_code == "publish_unconfirmed"
                for a in results
            )
            if (
                uncertain
                or interrupted
                or execution_error in {"executor_lost", "executor_interrupted"}
            ):
                status = "unknown"
            elif statuses & {"queued", "dispatched"}:
                status = "running"
            elif statuses & {"blocked", "failed"} or execution_error:
                status = "partial" if "applied" in statuses else "refused"
            elif expired:
                status = "partial" if "applied" in statuses else "expired"
            else:
                status = "running"
        return SceneRunRead(
            id=run.id,
            house_id=house_id,
            scene_id=run.scene_id,
            scene_revision=run.scene_revision,
            created_at=utc(run.created_at),
            dispatch_deadline=utc(run.dispatch_deadline),
            status=status,
            execution_mode=run.execution_mode,
            execution_error=execution_error,
            actions=results,
        )

    async def dispatch(
        self, house_id, run_id, position, user_id, *, executor_token=None
    ):
        run = self.row(house_id, run_id)
        if run.requested_by != user_id:
            raise EntityNotFoundError("Resource not found")
        if run.execution_mode == "server":
            if executor_token is None or run.executor_token != executor_token:
                raise ConflictError("Server runs cannot be dispatched by the client")
            self.require_authority(house_id, user_id)
            from app.services.scene_run_admission import require_deadline

            require_deadline(self.session, run_id, self.now())
        view = self.get(house_id, run_id)
        if position < 0 or position >= len(view.actions):
            raise EntityNotFoundError("Resource not found")
        action = view.actions[position]
        if action.receipt is not None or action.status in {
            "dispatch_unknown",
            "blocked",
        }:
            return view  # Retry retrieves evidence; never reissues a claimed action.
        if action.status != "ready":
            raise ConflictError("Action is not ready for dispatch")
        # Acquire the action lock first: a predicate evaluated before waiting
        # for a row lock would itself be stale when the UPDATE eventually runs.
        self.session.scalar(
            select(SceneRunActionORM.run_id)
            .where(
                SceneRunActionORM.run_id == run_id,
                SceneRunActionORM.position == position,
            )
            .with_for_update()
        )
        claimed = self.session.execute(
            update(SceneRunActionORM)
            .where(
                SceneRunActionORM.run_id == run_id,
                SceneRunActionORM.position == position,
                SceneRunActionORM.claimed.is_(False),
                exists(
                    select(SceneRunORM.id).where(
                        SceneRunORM.id == run_id,
                        SceneRunORM.dispatch_deadline > self.now(),
                        deadline_predicate(self.session),
                        lease_predicate(self.session),
                    )
                ),
            )
            .values(claimed=True)
        )
        self.session.commit()  # Durable claim BEFORE entering any network-capable code.
        if claimed.rowcount != 1:
            return self.get(house_id, run_id)
        try:
            await CommandService(
                CommandRepository(self.session), self.transport, now=self.now
            ).submit(
                house_id,
                action.device_id,
                user_id,
                self.action_key(run_id, position),
                action.requested_on,
                scene_action=(run_id, position),
            )
        except (
            ConflictError,
            InvalidReferenceError,
            UnavailableError,
            EntityNotFoundError,
        ):
            self.session.rollback()
            self.session.execute(
                update(SceneRunActionORM)
                .where(
                    SceneRunActionORM.run_id == run_id,
                    SceneRunActionORM.position == position,
                )
                .values(failure_code="dispatch_blocked")
            )
            self.session.commit()
        # Cancellation/unexpected errors retain the claim; recovery never guesses/replays.
        return self.get(house_id, run_id)
