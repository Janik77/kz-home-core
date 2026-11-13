"""Durable, fail-closed command evidence. No replay worker or assumed delivery."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from app.core.errors import (
    ConflictError,
    EntityNotFoundError,
    InvalidReferenceError,
    UnavailableError,
)
from app.models import DeviceCommandORM, SceneRunReceiptORM
from app.repositories.command_repository import CommandRepository, PENDING
from app.repositories.device_repository import DeviceRepository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.commands import CommandRead
from app.services.onboarding_service import PhysicalBindingGuard
from app.services.state_validator import StateValidator
from app.services.device_service import DeviceService
from app.transports.base import Transport
from app.transports.mqtt_models import AckEnvelope, StateEnvelope
from app.services.command_clock import COMMAND_LIFETIME, command_clock
from app.models import ControlCommandORM
from app.services.control_authority import default_control_authority

PUBLISH_TIMEOUT_SECONDS = 5


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class CommandService:
    def __init__(
        self,
        repository: CommandRepository,
        transport: Transport | None = None,
        *,
        now: Callable[[], datetime] = command_clock.now,
        control_authority=None,
    ):
        self.repository = repository
        self.transport = transport
        self.now = now
        self.control_authority = (control_authority or getattr(transport, "control_authority", None)
                                  or default_control_authority)

    def expire(self, row: DeviceCommandORM) -> None:
        if row.status not in PENDING:
            return
        control = self.repository.session.get(ControlCommandORM, row.command_id)
        expired = (self.control_authority.expired(control) if control is not None
                   else utc(row.expires_at) <= self.now())
        if expired:
            if control is None:
                self.finish(row, "timed_out", "deadline_exceeded")
            else:
                row.status, row.failure_code = "timed_out", "deadline_exceeded"
                row.updated_at = row.completed_at = self.control_authority.clock.logical_now()

    def finish(
        self, row: DeviceCommandORM, status: str, code: str | None = None
    ) -> None:
        row.status, row.failure_code = status, code
        row.updated_at = row.completed_at = self.event_time(row)

    def event_time(self, row: DeviceCommandORM) -> datetime:
        if self.repository.session.get(ControlCommandORM, row.command_id) is not None:
            return self.control_authority.clock.logical_now()
        return self.now()

    def get(self, house_id: str, command_id: str) -> CommandRead:
        row = self.repository.find(house_id=house_id, command_id=command_id)
        if row is None:
            raise EntityNotFoundError("Resource not found")
        self.expire(row)
        self.repository.save()
        return CommandRead.model_validate(row)

    async def submit(
        self, house_id: str, device_id: str, user_id: str, key: str, on: bool,
        *, scene_action: tuple[str, int] | None = None
    ) -> CommandRead:
        session = self.repository.session
        PhysicalBindingGuard(PhysicalDeviceRepository(session)).require_active(
            device_id, lock=True
        )
        device = self.repository.lock_device(device_id)
        if DeviceRepository(session).house_id(device_id) != house_id:
            raise ConflictError("Device location changed; retry authorization")
        digest = sha256(key.encode("ascii")).hexdigest()
        previous = self.repository.find(
            house_id=house_id, requested_by=user_id, idempotency_hash=digest
        )
        if previous is not None:
            if scene_action is not None:
                link = session.get(SceneRunReceiptORM, scene_action)
                if link is None or link.command_id != previous.command_id:
                    raise ConflictError("Command key is not linked to this scene action")
            if previous.device_id != device_id or previous.requested_on != on:
                raise ConflictError("Idempotency key was used for another intent")
            self.expire(previous)
            self.repository.save()
            return CommandRead.model_validate(previous)
        PhysicalBindingGuard(PhysicalDeviceRepository(session)).require_command_contract(
            device_id
        )
        if device.type != "relay" or "on_off" not in device.capabilities:
            raise InvalidReferenceError("Only relay on_off commands are supported")
        StateValidator().validate_command(DeviceService._read(device), {"on": on})
        pending = self.repository.pending(device_id)
        if pending is not None:
            self.expire(pending)
            if pending.status in PENDING:
                raise ConflictError("A command is already pending for this device")
            session.flush()  # Release the partial unique index inside this transaction.
        if not device.online:
            raise ConflictError("Device is reported offline")
        if self.transport is None or not getattr(self.transport, "connected", False):
            raise UnavailableError("MQTT transport is unavailable")
        await self.transport.authorize_command(house_id, device_id)
        now = self.now()
        row = DeviceCommandORM(
            command_id=str(uuid4()),
            correlation_id=str(uuid4()),
            house_id=house_id,
            device_id=device_id,
            requested_by=user_id,
            idempotency_hash=digest,
            requested_on=on,
            status="queued",
            created_at=now,
            updated_at=now,
            expires_at=now + COMMAND_LIFETIME,
            dispatch_started_at=now,
        )
        session.add(row)
        if scene_action is not None:
            session.flush()
            session.add(SceneRunReceiptORM(run_id=scene_action[0], position=scene_action[1], command_id=row.command_id))
        self.repository.save()  # IDs/intent/deadline durable BEFORE network I/O; release locks.
        identifier = row.command_id
        try:
            await asyncio.wait_for(
                self.transport.send_command(
                    house_id,
                    device_id,
                    {"on": on},
                    command_id=identifier,
                    correlation_id=row.correlation_id,
                    timestamp=utc(row.created_at),
                    expires_at=utc(row.expires_at),
                ),
                timeout=PUBLISH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            # Delivery may have occurred. The persisted deadline/idempotency survives cancellation.
            raise
        except Exception:
            current = self.repository.find(command_id=identifier)
            self.expire(current)
            if current.status in PENDING:
                self.finish(current, "failed", "publish_unconfirmed")
            self.repository.save()
        else:
            current = self.repository.find(command_id=identifier)
            self.expire(current)
            if current.status in PENDING:
                current.status = "dispatched"
                current.dispatched_at = current.dispatched_at or self.now()
                current.updated_at = self.now()
            self.repository.save()
        return self.get(house_id, identifier)

    def eligible(self, row: DeviceCommandORM | None, retained: bool) -> bool:
        if row is None:
            return False
        self.expire(row)
        return (
            not retained
            and row.status in PENDING
            and row.dispatch_started_at is not None
        )

    def confirm(self, row: DeviceCommandORM) -> None:
        if (
            row.ack_status == "applied"
            and self.permit_evidence(row)
            and row.state_received_at is not None
            and row.reported_on == row.requested_on
        ):
            self.finish(row, "applied")

    def permit_evidence(self, row: DeviceCommandORM) -> bool:
        control = self.repository.session.get(ControlCommandORM, row.command_id)
        return control is None or control.permit_binding_sha256 is not None

    def ack(
        self,
        house_id: str,
        device_id: str,
        envelope: AckEnvelope,
        retained: bool = False,
    ) -> None:
        row = self.repository.find(
            command_id=envelope.command_id,
            correlation_id=envelope.correlation_id,
            house_id=house_id,
            device_id=device_id,
        )
        if self.eligible(row, retained):
            if envelope.status == "applied" and not self.permit_evidence(row):
                self.repository.save()
                return
            # ACK accepted cannot regress applied evidence. Negative ACK before verified
            # completion fails closed; terminal lifecycle records are immutable.
            if envelope.status in ("rejected", "failed") or (
                row.ack_status != "applied" and row.ack_status != envelope.status
            ):
                row.ack_status = envelope.status
                row.ack_received_at = self.event_time(row)
                row.updated_at = self.event_time(row)
                if envelope.status in ("rejected", "failed"):
                    self.finish(row, "failed", "device_" + envelope.status)
                else:
                    row.status = "dispatched"
                    row.dispatched_at = row.dispatched_at or self.event_time(row)
                    self.confirm(row)
        self.repository.save()

    def state(
        self,
        house_id: str,
        device_id: str,
        envelope: StateEnvelope,
        retained: bool = False,
    ) -> None:
        row = self.repository.find(
            correlation_id=envelope.correlation_id,
            house_id=house_id,
            device_id=device_id,
        )
        if (self.eligible(row, retained) and self.permit_evidence(row)
                and type(envelope.state.get("on")) is bool):
            row.reported_on = envelope.state["on"]
            row.state_received_at = self.event_time(row)
            row.updated_at = self.event_time(row)
            self.confirm(row)
        self.repository.save()
