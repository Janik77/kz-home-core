"""One conditional authority; physical admission is denied in shipped wiring."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import select, update, inspect
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, UnavailableError
from app.models import ControlCommandORM, DeviceCommandORM
from app.models.control_command import PROFILE
from app.repositories.command_repository import CommandRepository, PENDING
from app.repositories.device_repository import DeviceRepository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.services.control_clock import ControlClock, LIFETIME_US, PERMIT_US
from app.transports.control_models import PermitRequest


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def closed_gate(session, house, device):
    raise ConflictError("Physical control remains closed pending joint acceptance")


@dataclass(frozen=True)
class ControlDelivery:
    payload: str
    command_id: str
    epoch: str
    deadline_tick: int
    valid_until_tick: int
    grant: bool


@dataclass(frozen=True)
class Window:
    deadline: int
    identity: tuple
    wire_sha256: str


class ControlAuthority:
    MAX_WINDOWS = 4096
    MAX_REPLIES = 1024

    def __init__(
        self,
        clock: ControlClock | None = None,
        gate: Callable[[Session, str, str], None] = closed_gate,
    ):
        self.clock = clock or ControlClock()
        self.gate = gate
        self.windows: dict[str, Window] = {}
        self.replies: dict[str, tuple[str, ControlDelivery]] = {}
        self.rates: dict[tuple[str, str], int] = {}
        self.global_second = -1
        self.global_count = 0
        self.recovery_initialized = False
        self.recovery_ids: frozenset[str] = frozenset()

    def initialize(self, session: Session) -> None:
        """Snapshot old pending records; start fence after the snapshot, not before it."""
        if self.recovery_initialized:
            return
        identifiers = list(
            session.scalars(
                select(ControlCommandORM.command_id)
                .join(
                    DeviceCommandORM,
                    DeviceCommandORM.command_id == ControlCommandORM.command_id,
                )
                .where(
                    DeviceCommandORM.status.in_(PENDING),
                    ControlCommandORM.authority_epoch != self.clock.epoch,
                )
                .limit(self.MAX_WINDOWS + 1)
            )
        )
        if len(identifiers) > self.MAX_WINDOWS:
            raise UnavailableError("Control recovery capacity reached")
        tick = self.clock.ticks()
        if self.clock.timer_fault or type(tick) is not int or tick < self.clock.last:
            self.clock.timer_fault = True
            raise UnavailableError("Control recovery timing is unavailable")
        self.clock.last = tick
        self.clock.fence_start = tick
        self.recovery_ids = frozenset(identifiers)
        self.recovery_initialized = True

    @staticmethod
    def identity(row):
        return (
            row.command_id,
            row.house_id,
            row.device_id,
            row.correlation_id,
            utc(row.created_at),
            utc(row.expires_at),
            row.requested_on,
        )

    def physical(self, session, house, device):
        item = PhysicalDeviceRepository(session).for_house(house, device)
        if (
            item.status != "active"
            or DeviceRepository(session).house_id(device) != house
        ):
            raise ConflictError("Physical control binding is not active")
        self.gate(session, house, device)

    def prepare(self, session: Session, row: DeviceCommandORM) -> None:
        """Create ledger + private candidate atomically, never adopt historical rows."""
        house, device, command_id = row.house_id, row.device_id, row.command_id
        self.physical(session, house, device)
        if not self.recovery_initialized:
            raise UnavailableError("Control recovery snapshot is unavailable")
        now, tick = self.clock.sample()
        repo = CommandRepository(session)
        if (
            not inspect(row).transient
            or row.status not in PENDING
            or repo.find(command_id=command_id) is not None
            or row.dispatch_started_at is not None
        ):
            raise ConflictError("Only a new control intent can be prepared")
        created, expires = utc(row.created_at), utc(row.expires_at)
        remaining = (expires - now) // timedelta(microseconds=1)
        lifetime = (expires - created) // timedelta(microseconds=1)
        if not created <= now < expires or not 0 < lifetime <= LIFETIME_US:
            raise ConflictError("Control candidate deadline is invalid")
        # Do not evict unresolved timing evidence. Exhaustion fails closed;
        # restart recovers old records through the qualified recovery fence.
        if len(self.windows) >= self.MAX_WINDOWS:
            terminal = session.scalars(
                select(DeviceCommandORM.command_id).where(
                    DeviceCommandORM.command_id.in_(self.windows),
                    DeviceCommandORM.status.not_in(PENDING),
                )
            )
            for identifier in terminal:
                self.windows.pop(identifier, None)
        if len(self.windows) >= self.MAX_WINDOWS:
            raise UnavailableError("Control authority capacity reached")
        payload = json.dumps(
            {
                "protocol_version": "v1",
                "execution_profile": PROFILE,
                "core_epoch": self.clock.epoch,
                "command_id": row.command_id,
                "correlation_id": row.correlation_id,
                "timestamp": created.isoformat(),
                "expires_at": expires.isoformat(),
                "state": {"on": row.requested_on},
            },
            separators=(",", ":"),
        )
        record = ControlCommandORM(
            command_id=command_id,
            execution_profile=PROFILE,
            authority_epoch=self.clock.epoch,
            wire_payload=payload,
            wire_sha256=digest(payload),
        )
        session.add(row)
        # Order the FK inserts explicitly without committing the ledger alone.
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            raise ConflictError("Control intent conflicts with existing data") from None
        session.add(record)
        repo.save()  # Exact bytes/hash and binding durable before any MQTT I/O.
        deadline = tick + remaining
        self.windows[command_id] = Window(
            deadline, self.identity(row), record.wire_sha256
        )

    def stage(
        self, session, house: str, device: str, command_id: str
    ) -> ControlDelivery:
        self.physical(session, house, device)
        now, tick = self.clock.sample()
        repo = CommandRepository(session)
        row = repo.find(command_id=command_id, house_id=house, device_id=device)
        record = session.get(ControlCommandORM, command_id)
        window = self.windows.get(command_id)
        if (
            row is None
            or record is None
            or window is None
            or record.authority_epoch != self.clock.epoch
            or row.status not in PENDING
            or row.dispatch_started_at is not None
            or window.identity != self.identity(row)
            or record.wire_sha256 != window.wire_sha256
            or digest(record.wire_payload) != window.wire_sha256
            or tick >= window.deadline
        ):
            raise ConflictError("Control candidate cannot be staged or replayed")
        row.dispatch_started_at = now
        repo.save()  # A cancelled publish must not make explicit replay possible.
        return ControlDelivery(
            record.wire_payload,
            command_id,
            self.clock.epoch,
            window.deadline,
            window.deadline,
            False,
        )

    def _limit(self, house, device, tick):
        self.rates = {
            key: value for key, value in self.rates.items() if tick - value < 1_000_000
        }
        second = tick // 1_000_000
        if second != self.global_second:
            self.global_second, self.global_count = second, 0
        if (
            (house, device) in self.rates
            or len(self.rates) >= 4096
            or self.global_count >= 100
        ):
            raise UnavailableError("Control request rate exceeded")
        self.rates[house, device] = tick
        self.global_count += 1

    def request(
        self, session, house: str, device: str, request: PermitRequest
    ) -> ControlDelivery:
        # Fresh authorization and clock checks precede even duplicate replies.
        self.physical(session, house, device)
        now, tick = self.clock.sample()
        row = CommandRepository(session).find(
            command_id=request.command_id, house_id=house, device_id=device
        )
        record = session.scalar(
            select(ControlCommandORM)
            .where(ControlCommandORM.command_id == request.command_id)
            .with_for_update()
        )
        window = self.windows.get(request.command_id)
        if (
            row is None
            or record is None
            or window is None
            or record.execution_profile != PROFILE
            or request.core_epoch != self.clock.epoch
            or record.authority_epoch != self.clock.epoch
            or request.intent_sha256 != record.wire_sha256
            or record.wire_sha256 != window.wire_sha256
            or digest(record.wire_payload) != record.wire_sha256
            or row.status not in PENDING
            or row.dispatch_started_at is None
            or window.identity != self.identity(row)
        ):
            raise ConflictError("Control request does not match a current candidate")
        if tick >= window.deadline:
            raise ConflictError("Control request expired")
        binding = digest(
            json.dumps(
                [
                    house,
                    device,
                    request.core_epoch,
                    request.boot_id,
                    request.session_id,
                    request.nonce,
                ]
            )
        )
        if record.permit_binding_sha256 is not None:
            previous = self.replies.get(row.command_id)
            if (
                record.permit_binding_sha256 == binding
                and previous is not None
                and previous[0] == binding
                and tick < previous[1].valid_until_tick
            ):
                return previous[1]
            raise ConflictError("A permit attempt was already consumed")
        self._limit(house, device, tick)
        self.replies = {
            key: value
            for key, value in self.replies.items()
            if value[1].valid_until_tick > tick
        }
        if len(self.replies) >= self.MAX_REPLIES:
            raise UnavailableError("Control reply capacity reached")
        remaining = min(PERMIT_US, window.deadline - tick) - 1  # Round/sample guard.
        if remaining <= 0:
            raise ConflictError("Control request expired")
        payload = json.dumps(
            {
                **request.model_dump(),
                "type": "permit_grant",
                "core_time": now.isoformat(),
                "expires_at": utc(row.expires_at).isoformat(),
                "remaining_us": remaining,
            },
            separators=(",", ":"),
        )
        # CAS is required even with row locks: SQLite ignores FOR UPDATE.
        try:
            changed = session.execute(
                update(ControlCommandORM)
                .where(
                    ControlCommandORM.command_id == row.command_id,
                    ControlCommandORM.permit_binding_sha256.is_(None),
                )
                .values(permit_binding_sha256=binding)
            ).rowcount
        except IntegrityError:
            session.rollback()
            raise ConflictError("Control nonce was already used") from None
        if changed != 1:
            session.rollback()
            raise ConflictError("A permit attempt was already consumed")
        CommandRepository(session).save()  # Consume before reply publication.
        reply = ControlDelivery(
            payload,
            row.command_id,
            self.clock.epoch,
            window.deadline,
            tick + remaining,
            True,
        )
        self.replies[row.command_id] = binding, reply
        return reply

    def validate_delivery(self, delivery: ControlDelivery):
        _, tick = self.clock.sample()
        if delivery.epoch != self.clock.epoch or tick >= min(
            delivery.valid_until_tick, delivery.deadline_tick
        ):
            raise ConflictError("Control delivery expired")

    def expired(self, record: ControlCommandORM) -> bool:
        if record.authority_epoch != self.clock.epoch:
            if (
                not self.recovery_initialized
                or record.command_id not in self.recovery_ids
            ):
                raise UnavailableError("Foreign control epoch has no recovery evidence")
            if not self.clock.recovered():
                raise UnavailableError("Old control epoch is inside recovery fence")
            return True
        window = self.windows.get(record.command_id)
        if window is None:
            raise UnavailableError("Original control timing cannot be recovered")
        return self.clock.tick() >= window.deadline


default_control_authority = ControlAuthority()
