"""Caller-driven, fail-closed software scene runs. Never automatically replay."""

from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select, update
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


class SceneRunService:
    def __init__(self, session, transport=None, *, now=command_clock.now):
        self.session, self.transport, self.now = session, transport, now

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

    def create(self, house_id, user_id, key, data):
        digest = sha256(key.encode("ascii")).hexdigest()
        criteria = dict(
            house_id=house_id, requested_by=user_id, idempotency_hash=digest
        )
        previous = self.session.scalar(select(SceneRunORM).filter_by(**criteria))
        if previous is not None:
            return self.replay(previous, data)
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
        now = self.now()
        row = SceneRunORM(
            id=str(uuid4()),
            scene_id=scene.id,
            scene_revision=scene.revision,
            created_at=now,
            dispatch_deadline=now + timedelta(minutes=5),
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
            return self.replay(previous, data)
        return self.get(house_id, row.id)

    def replay(self, row, data):
        if (
            row.scene_id != data.scene_id
            or row.scene_revision != data.expected_revision
        ):
            raise ConflictError("Idempotency key was used for another scene intent")
        return self.get(row.house_id, row.id)

    def get(self, house_id, run_id):
        run = self.row(house_id, run_id)
        results = []
        predecessor_ok = True
        stopped = False
        expired = utc(run.dispatch_deadline) <= self.now()
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
                status = "dispatch_unknown"
            elif stopped or expired:
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
        return SceneRunRead(
            id=run.id,
            house_id=house_id,
            scene_id=run.scene_id,
            scene_revision=run.scene_revision,
            created_at=utc(run.created_at),
            dispatch_deadline=utc(run.dispatch_deadline),
            status=status,
            actions=results,
        )

    async def dispatch(self, house_id, run_id, position, user_id):
        run = self.row(house_id, run_id)
        if run.requested_by != user_id:
            raise EntityNotFoundError("Resource not found")
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
        claimed = self.session.execute(
            update(SceneRunActionORM)
            .where(
                SceneRunActionORM.run_id == run_id,
                SceneRunActionORM.position == position,
                SceneRunActionORM.claimed.is_(False),
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
