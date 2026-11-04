"""Transaction authority for create/read/cancel only. Never claims inventory."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
import json
from secrets import token_urlsafe
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, EntityNotFoundError
from app.models import (
    HouseMembershipORM,
    HouseORM,
    ProvisioningSessionORM,
    UserORM,
)
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.provisioning import ProvisioningStart, ProvisioningPublicStatus
from app.security import Permission, ROLE_PERMISSIONS
from app.services.onboarding_service import claim_digest
from app.services.provisioning_state import (
    ProvisioningState,
    ProvisioningEvent,
    transition,
)

TTL = timedelta(minutes=5)


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ProvisioningService:
    def __init__(self, session: Session):
        self.session = session
        self.physical = PhysicalDeviceRepository(session)

    @contextmanager
    def atomic(self):
        try:
            yield
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            raise ConflictError(
                "Provisioning operation conflicts with existing data"
            ) from None
        except Exception:
            self.session.rollback()
            raise

    def _authorize(self, house_id: str, actor_id: str):
        # Same house-first order as existing structural/onboarding operations.
        if (
            self.session.scalar(
                select(HouseORM.id).where(HouseORM.id == house_id).with_for_update()
            )
            is None
        ):
            raise EntityNotFoundError("Resource not found")
        membership = self.session.scalar(
            select(HouseMembershipORM)
            .where(
                HouseMembershipORM.house_id == house_id,
                HouseMembershipORM.user_id == actor_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        user = self.session.get(UserORM, actor_id, populate_existing=True)
        if user is None or not user.is_active or membership is None:
            raise EntityNotFoundError("Resource not found")
        if Permission.DEVICE_ONBOARD not in ROLE_PERMISSIONS.get(membership.role, ()):
            raise HTTPException(403, "Insufficient house permission")

    def _now(self) -> datetime:
        # Evaluate after lock acquisition, not transaction start (now() on PG).
        if self.session.get_bind().dialect.name == "postgresql":
            return self.session.scalar(select(func.clock_timestamp()))
        return datetime.now(UTC)

    def _state(self, row) -> ProvisioningState:
        return ProvisioningState(
            session_id=UUID(row.id),
            house_id=row.house_id,
            device_id=row.device_id,
            room_id=row.room_id,
            created_at=utc(row.created_at),
            expires_at=utc(row.expires_at),
            challenge_hash=row.challenge_hash,
            status=row.status,
            revision=row.revision,
        )

    def _apply(self, row, event, now, **values):
        new = transition(
            self._state(row), event, expected_revision=row.revision, now=now
        )
        result = self.session.execute(
            update(ProvisioningSessionORM)
            .where(
                ProvisioningSessionORM.id == row.id,
                ProvisioningSessionORM.revision == row.revision,
                ProvisioningSessionORM.status == row.status,
            )
            .values(status=new.status.value, revision=new.revision, **values)
        )
        if result.rowcount != 1:
            raise ConflictError("Provisioning revision conflict")
        self.session.refresh(row)

    def _expire(self, row, now):
        if row.status == "discovered" and now >= utc(row.expires_at):
            self._apply(row, ProvisioningEvent.EXPIRE, now)

    def create(
        self, house_id: str, actor_id: str, data: ProvisioningStart, key: UUID
    ) -> ProvisioningPublicStatus:
        # Include the high-entropy code digest, never serialize raw SecretStr.
        body_hash = digest(
            json.dumps(
                {
                    "hardware_id": data.hardware_id,
                    "room_id": data.room_id,
                    "name": data.name,
                    "claim_digest": claim_digest(data.claim_code.get_secret_value()),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        with self.atomic():
            self._authorize(house_id, actor_id)
            previous = self.session.scalar(
                select(ProvisioningSessionORM)
                .where(
                    ProvisioningSessionORM.house_id == house_id,
                    ProvisioningSessionORM.actor_id == actor_id,
                    ProvisioningSessionORM.create_key_hash == digest(str(key)),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if previous is not None:
                if not compare_digest(previous.request_hash, body_hash):
                    raise ConflictError("Idempotency key conflicts with request")
                self._expire(previous, self._now())
                result = self._state(previous).public_status()
            else:
                if self.physical.structure_house("room", data.room_id) != house_id:
                    raise EntityNotFoundError("Resource not found")
                item = self.physical.by_hardware(data.hardware_id)
                if (
                    item is None
                    or item.status != "unprovisioned"
                    or not compare_digest(
                        item.claim_code_hash or "",
                        claim_digest(data.claim_code.get_secret_value()),
                    )
                ):
                    raise EntityNotFoundError("Resource not found")
                active = self.session.scalar(
                    select(ProvisioningSessionORM)
                    .where(
                        ProvisioningSessionORM.device_id == item.device_id,
                        ProvisioningSessionORM.status == "discovered",
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                now = self._now()
                if active is not None:
                    self._expire(active, now)
                    if active.status == "discovered":
                        # Do not disclose which other house/actor owns the attempt.
                        raise ConflictError("Device has an active provisioning attempt")
                row = ProvisioningSessionORM(
                    id=str(uuid4()),
                    house_id=house_id,
                    room_id=data.room_id,
                    device_id=item.device_id,
                    actor_id=actor_id,
                    name=data.name,
                    status="discovered",
                    revision=0,
                    created_at=now,
                    expires_at=now + TTL,
                    challenge_hash=digest(token_urlsafe(32)),
                    token_hash=digest(token_urlsafe(32)),
                    create_key_hash=digest(str(key)),
                    request_hash=body_hash,
                )
                self.session.add(row)
                self.session.flush()
                result = self._state(row).public_status()
        return result

    def _load(self, house_id, actor_id, session_id):
        self._authorize(house_id, actor_id)
        row = self.session.scalar(
            select(ProvisioningSessionORM)
            .where(
                ProvisioningSessionORM.id == str(session_id),
                ProvisioningSessionORM.house_id == house_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise EntityNotFoundError("Resource not found")
        return row

    def get(
        self, house_id: str, actor_id: str, session_id: UUID
    ) -> ProvisioningPublicStatus:
        with self.atomic():
            row = self._load(house_id, actor_id, session_id)
            self._expire(row, self._now())
            result = self._state(row).public_status()
        return result

    def cancel(
        self, house_id: str, actor_id: str, session_id: UUID, revision: int, key: UUID
    ) -> ProvisioningPublicStatus:
        conflict = False
        with self.atomic():
            row = self._load(house_id, actor_id, session_id)
            now = self._now()
            self._expire(row, now)
            if (
                row.cancel_key_hash == digest(str(key))
                and row.cancel_actor_id == actor_id
            ):
                if row.cancel_revision != revision:
                    raise ConflictError("Idempotency key conflicts with request")
            elif row.revision != revision or row.status != "discovered":
                conflict = True
            else:
                self._apply(
                    row,
                    ProvisioningEvent.CANCEL,
                    now,
                    cancel_key_hash=digest(str(key)),
                    cancel_actor_id=actor_id,
                    cancel_revision=revision,
                )
            result = self._state(row).public_status()
        # Expiry is durable even when cancel returns conflict.
        if conflict:
            raise ConflictError("Provisioning revision or status conflict")
        return result
