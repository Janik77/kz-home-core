"""Pure immutable attempt state; no ownership, credentials or control authority.

PROOF_VERIFIED and CONNECTED are trusted internal events, never public flags.
This module does not verify signatures. The future adapter must do that first.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest
import re
from uuid import UUID

from app.schemas.provisioning import (
    ProvisioningErrorCode,
    ProvisioningPublicStatus,
    ProvisioningStatus as Status,
)


class ProvisioningEvent(StrEnum):
    PROOF_VERIFIED = "proof_verified"
    CONFIRM = "confirm"
    CONNECTED = "connected"
    FAIL = "fail"
    CANCEL = "cancel"
    EXPIRE = "expire"


class ProvisioningConflict(ValueError):
    """Invalid transition, replay or stale revision; contains no request input."""


class ProvisioningExpired(ProvisioningConflict):
    """Caller must materialize EXPIRE, never proceed with the requested action."""


TERMINAL = frozenset({Status.CONNECTED, Status.ERROR, Status.EXPIRED, Status.CANCELLED})


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware time required")


def challenge_digest(challenge: str) -> str:
    # Format cannot prove entropy. Future issuer must use 32 CSPRNG bytes.
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        raise ValueError("Invalid challenge format")
    return sha256(challenge.encode("ascii")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ProvisioningState:
    session_id: UUID
    house_id: str
    device_id: str
    room_id: str
    created_at: datetime
    expires_at: datetime
    challenge_hash: str = field(repr=False)
    status: Status = Status.DISCOVERED
    revision: int = 0
    proof_consumed: bool = False
    error_code: ProvisioningErrorCode | None = None

    def __post_init__(self) -> None:
        _aware(self.created_at)
        _aware(self.expires_at)
        if self.expires_at <= self.created_at:
            raise ValueError("Expiry must follow creation")
        if not re.fullmatch(r"[0-9a-f]{64}", self.challenge_hash):
            raise ValueError("Invalid challenge digest")
        if type(self.proof_consumed) is not bool:
            raise ValueError("Invalid proof state")
        if self.status == Status.DISCOVERED and self.proof_consumed:
            raise ValueError("Discovered attempt cannot have consumed proof")
        if (
            self.status
            in {Status.AWAITING_CONFIRMATION, Status.REGISTERED, Status.CONNECTED}
            and not self.proof_consumed
        ):
            raise ValueError("Verified proof required")
        if (self.status == Status.ERROR) != (self.error_code is not None):
            raise ValueError("Error code must match error status")
        self.public_status()  # Validate IDs/revision without importing ORM models.

    def public_status(self) -> ProvisioningPublicStatus:
        return ProvisioningPublicStatus(
            session_id=self.session_id,
            house_id=self.house_id,
            device_id=self.device_id,
            room_id=self.room_id,
            status=self.status,
            revision=self.revision,
            expires_at=self.expires_at,
            error_code=self.error_code,
        )


def transition(
    state: ProvisioningState,
    event: ProvisioningEvent,
    *,
    expected_revision: int,
    now: datetime,
    challenge: str | None = None,
    error_code: ProvisioningErrorCode | None = None,
) -> ProvisioningState:
    """Return a new snapshot or fail without modifying the original.

    Revision matching is only an in-memory contract, NOT a database CAS/lock.
    CONFIRM models completed registration; a future transaction must commit
    claim + snapshot + audit together. No such operation occurs here.
    """
    _aware(now)
    if type(expected_revision) is not int or expected_revision != state.revision:
        raise ProvisioningConflict("Revision conflict")
    if not isinstance(event, ProvisioningEvent):
        raise ProvisioningConflict("Unknown event")
    if now < state.created_at:
        raise ProvisioningConflict("Time precedes creation")
    if state.status in TERMINAL:
        raise ProvisioningConflict("Attempt is terminal")
    if now >= state.expires_at and event != ProvisioningEvent.EXPIRE:
        raise ProvisioningExpired("Attempt expired")
    if challenge is not None and event != ProvisioningEvent.PROOF_VERIFIED:
        raise ProvisioningConflict("Unexpected challenge")
    if error_code is not None and event != ProvisioningEvent.FAIL:
        raise ProvisioningConflict("Unexpected error code")

    target = None
    consumed = state.proof_consumed
    if event == ProvisioningEvent.PROOF_VERIFIED:
        if state.status != Status.DISCOVERED or consumed:
            raise ProvisioningConflict("Proof already consumed")
        if challenge is None or not compare_digest(
            state.challenge_hash, challenge_digest(challenge)
        ):
            raise ProvisioningConflict("Challenge mismatch")
        target, consumed = Status.AWAITING_CONFIRMATION, True
    elif (
        event == ProvisioningEvent.CONFIRM
        and state.status == Status.AWAITING_CONFIRMATION
    ):
        target = Status.REGISTERED
    elif event == ProvisioningEvent.CONNECTED and state.status == Status.REGISTERED:
        target = Status.CONNECTED
    elif event == ProvisioningEvent.CANCEL:
        target = Status.CANCELLED
    elif event == ProvisioningEvent.FAIL and isinstance(
        error_code, ProvisioningErrorCode
    ):
        target = Status.ERROR
    elif event == ProvisioningEvent.EXPIRE and now >= state.expires_at:
        target = Status.EXPIRED
    if target is None:
        raise ProvisioningConflict("Transition not allowed")
    return replace(
        state,
        status=target,
        revision=state.revision + 1,
        proof_consumed=consumed,
        error_code=error_code,
    )
