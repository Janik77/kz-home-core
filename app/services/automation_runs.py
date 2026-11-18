"""Device-state rules use the SAME durable claims, command receipts and executor.

First slice deliberately forbids automation-to-automation chaining. No work is
resumed on startup. Legacy definitions require explicit owner re-authorization.
"""

from datetime import datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import select
from app.core.errors import (
    ConflictError,
    InvalidReferenceError,
    EntityNotFoundError,
    UnavailableError,
)
from app.models import (
    AutomationORM,
    SceneRunORM,
    SceneRunReceiptORM,
    DeviceCommandORM,
    HouseORM,
    UserORM,
)
from app.repositories.automation_repository import AutomationRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.auth_repository import MembershipRepository
from app.services.auth_service import AuthorizationService
from app.services.scene_run_service import SceneRunService
from app.services.command_service import utc
from app.security import Permission

COOLDOWN = timedelta(seconds=30)
MAX_STATE_AGE = timedelta(minutes=2)


class AutomationRunService(SceneRunService):
    source_kind = "automation"

    def definition(self, entity_id):
        return AutomationRepository(self.session).get(entity_id)

    def lock_definition(self, entity_id):
        self.session.scalar(
            select(AutomationORM).where(AutomationORM.id == entity_id).with_for_update()
        )

    def require_authority(self, house_id, user_id):
        user = self.session.get(UserORM, user_id, populate_existing=True)
        auth = AuthorizationService(MembershipRepository(self.session))
        membership = auth.get_house_membership(user_id, house_id)
        if (
            user is None
            or not user.is_active
            or not all(
                auth.has_permission(membership, p)
                for p in (Permission.AUTOMATION_MANAGE, Permission.DEVICE_CONTROL)
            )
        ):
            raise ConflictError("Automation authorization revoked")

    def before_dispatch(self, run):
        super().before_dispatch(run)
        rule = self.session.get(AutomationORM, run.scene_id, populate_existing=True)
        if (
            rule is None
            or not rule.enabled
            or rule.revision != run.scene_revision
            or rule.authorized_by != run.requested_by
            or rule.house_id != run.house_id
        ):
            raise ConflictError("Automation disabled or definition changed")
        if run.schedule_timezone_revision is not None:
            house = self.session.get(HouseORM, run.house_id, populate_existing=True)
            if (
                house is None
                or house.timezone_revision != run.schedule_timezone_revision
            ):
                raise ConflictError("Schedule house timezone changed")
        self.validate_rule(rule)
        if not self.conditions_match(rule):
            raise ConflictError("Automation conditions changed")
        self.require_authority(run.house_id, run.requested_by)
        for action in self.actions(run.id):
            if action.kind == "durable_wait":
                continue
            self.fresh_device(action.device_id, run.house_id)

    def fresh_device(self, device_id, house_id, field="on"):
        devices = DeviceRepository(self.session)
        device = devices.get(device_id)
        if devices.house_id(device_id) != house_id:
            raise ConflictError("Device belongs to another house")
        from app.services.numeric_state import NUMERIC_RANGES, valid_number

        observed = device.state_observed_at
        if field in NUMERIC_RANGES:
            if field not in device.capabilities or not valid_number(
                field, device.state.get(field)
            ):
                raise InvalidReferenceError("Unsupported numeric device state")
            value = (device.numeric_observed_at or {}).get(field)
            observed = datetime.fromisoformat(value) if value else None
        elif (
            field != "on"
            or device.type != "relay"
            or "on_off" not in device.capabilities
            or type(device.state.get("on")) is not bool
        ):
            raise InvalidReferenceError("Only Boolean relay states are supported")
        if (
            not device.online
            or observed is None
            or self.now() - utc(observed) > MAX_STATE_AGE
            or utc(observed) > self.now() + timedelta(seconds=5)
        ):
            raise ConflictError("Device state is offline or stale")
        return device

    def validate_rule(self, rule):
        from app.services.wait_contract import wait_plan

        try:
            wait_plan(rule.actions)
        except ValueError as error:
            raise InvalidReferenceError(str(error)) from error
        checks = [rule.trigger, *rule.conditions]
        if not 1 <= len(rule.actions) <= 16:
            raise InvalidReferenceError(
                "Automation requires 1 to 16 Boolean relay actions"
            )
        from app.schemas import AutomationTrigger, DeviceStateCondition

        for i, condition in enumerate(checks):
            kind = condition.get("type", "device_state")
            if i == 0 and kind == "schedule":
                from app.schemas import ScheduleTrigger

                try:
                    ScheduleTrigger.model_validate(condition)
                except ValueError as error:
                    raise InvalidReferenceError(
                        "Unsupported schedule definition"
                    ) from error
                house = self.session.get(
                    HouseORM, rule.house_id, populate_existing=True
                )
                if house is None or not house.timezone:
                    raise InvalidReferenceError("House timezone required")
                continue
            if kind == ("numeric_threshold" if i == 0 else "numeric_state"):
                try:
                    (
                        AutomationTrigger if i == 0 else DeviceStateCondition
                    ).model_validate(condition)
                except ValueError as error:
                    raise InvalidReferenceError(
                        "Unsupported numeric definition"
                    ) from error
            elif (
                kind != "device_state"
                or condition.get("field") != "on"
                or condition.get("operator", "eq") not in {"eq", "neq"}
                or type(condition.get("value")) is not bool
            ):
                raise InvalidReferenceError(
                    "Only explicit numeric or Boolean device-state conditions are supported"
                )
            self.fresh_device(condition["device_id"], rule.house_id, condition["field"])
        for action in rule.actions:
            if action.get("type") == "durable_wait":
                continue
            if (
                action.get("type", "device_state") != "device_state"
                or set(action.get("state", {})) != {"on"}
                or type(action["state"]["on"]) is not bool
            ):
                raise InvalidReferenceError("Only Boolean relay actions are supported")
            self.fresh_device(action["device_id"], rule.house_id)

    def conditions_match(self, rule):
        from app.services.automation_service import compare

        return all(
            compare(
                self.fresh_device(c["device_id"], rule.house_id, c["field"]).state[
                    c["field"]
                ],
                c.get("operator", "eq"),
                c["value"],
            )
            for c in rule.conditions
        )

    async def dry_run(self, rule):
        issues = []
        matched = False
        if not rule.enabled:
            issues.append("rule_disabled")
        try:
            self.validate_rule(rule)
            matched = self.conditions_match(rule)
            if not matched:
                issues.append("conditions_not_met")
            if not rule.authorized_by:
                issues.append("authorization_required")
            else:
                self.require_authority(rule.house_id, rule.authorized_by)
            if self.transport is None or not getattr(
                self.transport, "connected", False
            ):
                issues.append("transport_unavailable")
            else:
                for action in rule.actions:
                    if action.get("type") == "durable_wait":
                        continue
                    from app.repositories.command_repository import CommandRepository

                    pending = CommandRepository(self.session).pending(
                        action["device_id"]
                    )
                    if pending is not None and utc(pending.expires_at) > self.now():
                        issues.append("command_pending")
                    await self.transport.authorize_command(
                        rule.house_id, action["device_id"]
                    )
        except (
            ConflictError,
            InvalidReferenceError,
            EntityNotFoundError,
            UnavailableError,
        ) as error:
            message = str(error).lower()
            code = "preflight_refused"
            if "house timezone" in message:
                code = "house_timezone_required"
            elif "offline" in message or "stale" in message:
                code = "state_unavailable"
            elif "authorization" in message:
                code = "authorization_revoked"
            elif (
                "boolean" in message or "actions" in message or "unsupported" in message
            ):
                code = "unsupported_definition"
            elif "software" in message:
                code = "destination_not_allowed"
            elif "not found" in message or "another house" in message:
                code = "device_unavailable"
            issues.append(code)
        return dict(
            automation_id=rule.id,
            revision=rule.revision,
            eligible=not issues,
            conditions_match=matched,
            issues=issues,
            actions=[
                dict(device_id=a["device_id"], requested_on=a["state"]["on"])
                for a in rule.actions
                if a.get("type", "device_state") == "device_state"
                and set(a.get("state", {})) == {"on"}
                and type(a["state"]["on"]) is bool
            ],
        )

    def view(self, run):
        result = self.get(run.house_id, run.id).model_dump()
        result["automation_id"] = result.pop("scene_id")
        result["automation_revision"] = result.pop("scene_revision")
        return result

    async def handle_event(self, event, executor):
        if (
            event.type != "device_state_changed"
            or not event.data.get("house_id")
            or not event.data.get("automation_eligible")
        ):
            return
        # Correlated reports caused by ANY automation cannot start another rule.
        causal = self.session.scalar(
            select(SceneRunORM.id)
            .join(SceneRunReceiptORM, SceneRunReceiptORM.run_id == SceneRunORM.id)
            .join(
                DeviceCommandORM,
                DeviceCommandORM.command_id == SceneRunReceiptORM.command_id,
            )
            .where(
                SceneRunORM.source_kind == "automation",
                DeviceCommandORM.correlation_id == event.correlation_id,
            )
            .limit(1)
        )
        if causal:
            return
        rules = list(
            self.session.scalars(
                select(AutomationORM).where(
                    AutomationORM.house_id == event.data["house_id"],
                    AutomationORM.enabled.is_(True),
                )
            )
        )
        from app.services.automation_service import compare

        for rule in rules:
            trigger = rule.trigger
            if (
                trigger.get("device_id") != event.data.get("device_id")
                or trigger.get("field") not in event.data.get("state", {})
                or (
                    trigger.get("type") != "numeric_threshold"
                    and not compare(
                        event.data["state"][trigger["field"]],
                        trigger.get("operator", "eq"),
                        trigger.get("value"),
                    )
                )
            ):
                continue
            if not rule.authorized_by:
                continue  # Existing rules must explicitly be adopted by an authorized editor.
            await self.admit(rule.id, event, executor)

    def consume_threshold(self, rule, event):
        """Persist edge consumption before command admission. Never replay on restart.

        Rejected edges are consumed too: a failed condition/permission is not a
        deferred intent. Fresh recovery must re-arm before another edge can run.
        """
        trigger = rule.trigger
        field = trigger["field"]
        from app.schemas import AutomationTrigger

        try:
            AutomationTrigger.model_validate(trigger)
            device = self.fresh_device(trigger["device_id"], rule.house_id, field)
        except (ValueError, ConflictError, InvalidReferenceError, EntityNotFoundError):
            self.session.rollback()
            return False
        stamp = event.data.get("numeric_observed_at", {}).get(field)
        if not stamp or stamp != device.numeric_observed_at.get(field):
            self.session.rollback()
            return False
        value = event.data["state"][field]
        if value != device.state.get(field):
            self.session.rollback()
            return False
        previous = rule.threshold_state or {}
        fresh_revision = previous.get("revision") == rule.revision
        if (
            fresh_revision
            and previous.get("observed_at")
            and utc(datetime.fromisoformat(stamp))
            <= utc(datetime.fromisoformat(previous["observed_at"]))
        ):
            self.session.rollback()
            return False
        limit, hysteresis = trigger["value"], trigger["hysteresis"]
        safe = (
            value >= limit + hysteresis
            if trigger["operator"] == "lt"
            else value <= limit - hysteresis
        )
        crossed = value < limit if trigger["operator"] == "lt" else value > limit
        # Gaps beyond freshness limit reset initialization instead of inferring an edge.
        continuous = (
            fresh_revision
            and bool(event.data.get("numeric_previous_observed_at", {}).get(field))
            and previous.get("observed_at")
            and utc(datetime.fromisoformat(stamp))
            - utc(datetime.fromisoformat(previous["observed_at"]))
            <= MAX_STATE_AGE
        )
        armed = bool(previous.get("armed")) if continuous else False
        fire = bool(continuous and armed and crossed)
        armed = safe or (armed and not fire)
        rule.threshold_state = {
            "revision": rule.revision,
            "armed": bool(armed),
            "observed_at": stamp,
        }
        self.session.commit()
        return fire

    async def admit(self, rule_id, event, executor, *, occurrence=None):
        rule = self.definition(rule_id)
        house_id, user_id = rule.house_id, rule.authorized_by
        # Serialize admission across processes, including cooldown and disable/edit.
        self.session.scalar(
            select(HouseORM).where(HouseORM.id == house_id).with_for_update()
        )
        self.lock_definition(rule_id)
        self.session.refresh(rule)
        if not rule.authorized_by or not rule.enabled:
            self.session.rollback()
            return
        user_id = rule.authorized_by
        if occurrence is not None and rule.trigger.get("type") != "schedule":
            self.session.rollback()
            return
        if occurrence is None and rule.trigger.get("type") == "schedule":
            self.session.rollback()
            return
        if rule.trigger.get("type") == "numeric_threshold":
            consumed_revision = rule.revision
            if not self.consume_threshold(rule, event):
                return
            # Consumption is committed before any effect. Reacquire admission locks
            # and reject an edit/disable in that intentional crash-safe boundary.
            self.session.scalar(
                select(HouseORM).where(HouseORM.id == house_id).with_for_update()
            )
            self.lock_definition(rule_id)
            self.session.refresh(rule)
            if (
                not rule.enabled
                or rule.revision != consumed_revision
                or rule.authorized_by != user_id
            ):
                self.session.rollback()
                return
        if occurrence is not None:
            from app.services.schedule_planner import consume_occurrence

            consumed_revision = rule.revision
            timezone_revision = consume_occurrence(self, rule, occurrence)
            if timezone_revision is None:
                return
            self.session.scalar(
                select(HouseORM).where(HouseORM.id == house_id).with_for_update()
            )
            self.lock_definition(rule_id)
            self.session.refresh(rule)
            house = self.session.get(HouseORM, house_id, populate_existing=True)
            if (
                not rule.enabled
                or rule.revision != consumed_revision
                or rule.authorized_by != user_id
                or house.timezone_revision != timezone_revision
            ):
                self.session.rollback()
                return
            key = (
                "schedule_"
                + sha256(
                    f"{rule.id}:{consumed_revision}:{timezone_revision}:{occurrence.isoformat()}".encode()
                ).hexdigest()
            )
        else:
            key = (
                "automation_"
                + sha256(
                    (
                        rule.id
                        + ":"
                        + event.correlation_id
                        + ":"
                        + str(event.data.get("device_id"))
                    ).encode()
                ).hexdigest()
            )
        digest = sha256(key.encode("ascii")).hexdigest()
        prior = self.session.scalar(
            select(SceneRunORM).where(
                SceneRunORM.source_kind == "automation",
                SceneRunORM.scene_id == rule.id,
                SceneRunORM.idempotency_hash == digest,
            )
        )
        if prior:
            self.session.rollback()
            return
        error = None
        latest = self.session.scalar(
            select(SceneRunORM)
            .where(
                SceneRunORM.source_kind == "automation", SceneRunORM.scene_id == rule.id
            )
            .order_by(SceneRunORM.created_at.desc())
            .limit(1)
        )
        if latest and self.now() - utc(latest.created_at) < COOLDOWN:
            error = "rate_limited"
        check = await self.dry_run(rule)
        if not check["issues"] and occurrence is None:
            current = self.fresh_device(
                rule.trigger["device_id"], house_id, rule.trigger["field"]
            )
            if current.state.get(rule.trigger["field"]) != event.data["state"].get(
                rule.trigger["field"]
            ):
                error = "event_superseded"
        if check["issues"] and error is None:
            error = check["issues"][0]
        if error:
            row = SceneRunORM(
                id=str(uuid4()),
                house_id=house_id,
                requested_by=user_id,
                idempotency_hash=digest,
                source_kind="automation",
                scene_id=rule.id,
                scene_revision=rule.revision,
                created_at=self.now(),
                dispatch_deadline=self.now(),
                execution_mode="server",
                execution_finished=True,
                execution_error=error,
            )
            self.session.add(row)
            self.session.commit()
            return
        token = str(uuid4())
        data = SimpleNamespace(scene_id=rule.id, expected_revision=rule.revision)
        try:
            view = await self.create_server(house_id, user_id, key, data, token)
        except (
            ConflictError,
            InvalidReferenceError,
            EntityNotFoundError,
            UnavailableError,
        ):
            self.session.rollback()
            # Admission can race with a new pending command or inventory change.
            # No network side effects have occurred at this point.
            prior = self.session.scalar(
                select(SceneRunORM).where(
                    SceneRunORM.source_kind == "automation",
                    SceneRunORM.scene_id == rule_id,
                    SceneRunORM.idempotency_hash == digest,
                )
            )
            if prior is not None:
                return
            row = SceneRunORM(
                id=str(uuid4()),
                house_id=house_id,
                requested_by=user_id,
                idempotency_hash=digest,
                source_kind="automation",
                scene_id=rule_id,
                scene_revision=data.expected_revision,
                created_at=self.now(),
                dispatch_deadline=self.now(),
                execution_mode="server",
                execution_finished=True,
                execution_error="admission_refused",
            )
            self.session.add(row)
            self.session.commit()
            return
        if occurrence is not None:
            row = self.session.get(SceneRunORM, view.id)
            row.schedule_timezone_revision = timezone_revision
            # Bound an admitted occurrence; never send a late queued schedule.
            if row.durable_plan:
                from app.models import SceneRunActionORM

                first = self.session.get(SceneRunActionORM, (row.id, 0))
                first.dispatch_before = min(
                    utc(first.dispatch_before), occurrence + timedelta(seconds=30)
                )
            else:
                row.dispatch_deadline = min(
                    utc(row.dispatch_deadline), occurrence + timedelta(seconds=30)
                )
            self.session.commit()
        executor.start(house_id, view.id, token)
