"""Draft provisioning contracts, deliberately not mounted as HTTP endpoints."""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.schemas.onboarding import DeviceClaim, TopicID

Revision = Annotated[int, Field(strict=True, ge=0)]


class ProvisioningStatus(StrEnum):
    DISCOVERED = "discovered"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    REGISTERED = "registered"
    CONNECTED = "connected"
    ERROR = "error"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ProvisioningErrorCode(StrEnum):
    PROOF_REJECTED = "proof_rejected"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    REGISTRATION_FAILED = "registration_failed"


class ProvisioningStart(DeviceClaim):
    """House comes from the future authorized route; reuse existing claim input.

    Like DeviceClaim, validation errors may contain input. A future route MUST
    redact validation errors and never log this request or its serialized body.
    """


class ProvisioningConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: Revision


class ProvisioningCancel(ProvisioningConfirm):
    pass


class ProvisioningPublicStatus(BaseModel):
    """Explicit allowlist: no proof, challenge, claim code or credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: UUID
    house_id: TopicID
    device_id: TopicID
    room_id: TopicID
    status: ProvisioningStatus
    revision: Revision
    expires_at: AwareDatetime
    error_code: ProvisioningErrorCode | None = None
