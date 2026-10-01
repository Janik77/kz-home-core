"""House binding only; broker credentials remain an external operator authority."""

import re
from hashlib import sha256
from hmac import compare_digest
from uuid import uuid4

from app.core.errors import ConflictError, EntityNotFoundError, InvalidReferenceError
from app.repositories.device_repository import DeviceRepository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.onboarding import (
    DeviceClaim,
    InventoryRegistration,
    PhysicalDeviceRead,
    TopicID,
)
from pydantic import TypeAdapter

# Trusted server-side profile. It describes a v1 relay contract, not verified
# electrical hardware. Other profiles require explicit review and tests.
PROFILES = {"esp32-c6-relay-v1": ("relay", ["on_off"])}


def claim_digest(code: str) -> str:
    # 32 random bytes encoded using token_urlsafe(32). Human passwords are not
    # acceptable claim codes. Only the digest is persisted; consume it on claim.
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
        raise InvalidReferenceError("Invalid claim code format")
    return sha256(code.encode("ascii")).hexdigest()


class OnboardingService:
    def __init__(self, repository: PhysicalDeviceRepository):
        self.repository = repository

    def register_inventory(self, data: InventoryRegistration) -> None:
        """Trusted operator entry point, deliberately absent from the HTTP API."""
        digest = claim_digest(data.claim_code.get_secret_value())
        with self.repository.atomic():
            if DeviceRepository(self.repository.session).exists(data.device_id):
                raise ConflictError("Device identity is already in use")
            self.repository.add_inventory(
                {
                    "device_id": data.device_id,
                    "hardware_id": data.hardware_id,
                    "hardware_model": data.hardware_model,
                    "protocol_version": "v1",
                    "status": "unprovisioned",
                    "claim_code_hash": digest,
                }
            )

    def claim(
        self, house_id: str, data: DeviceClaim, actor_id: str
    ) -> PhysicalDeviceRead:
        TypeAdapter(TopicID).validate_python(house_id)
        with self.repository.atomic():
            self.repository.lock_houses([house_id])
            item = self.repository.by_hardware(data.hardware_id)
            # No global inventory enumeration, including already owned foreign IDs.
            if item is None or (
                item.house_id is not None and item.house_id != house_id
            ):
                raise EntityNotFoundError("Resource not found")
            if item.status != "unprovisioned":
                raise ConflictError("Physical device is already claimed")
            digest = claim_digest(data.claim_code.get_secret_value())
            if not compare_digest(item.claim_code_hash or "", digest):
                raise EntityNotFoundError("Resource not found")
            if self.repository.structure_house("room", data.room_id) != house_id:
                raise EntityNotFoundError("Resource not found")
            device_type, capabilities = PROFILES[item.hardware_model]
            self.repository.add_device(
                {
                    "id": item.device_id,
                    "room_id": data.room_id,
                    "name": data.name,
                    "type": device_type,
                    "capabilities": list(capabilities),
                    "state": {},
                    "online": False,
                    "metadata_": {},
                }
            )
            self.repository.claim(item, house_id)
            self._audit(item.device_id, house_id, actor_id, "provisioning")
            self.repository.session.flush()
            result = PhysicalDeviceRead.model_validate(item)
        return result

    def transition(
        self, house_id: str, device_id: str, target: str, actor_id: str
    ) -> PhysicalDeviceRead:
        with self.repository.atomic():
            self.repository.lock_houses([house_id])
            item = self.repository.for_house(house_id, device_id)
            if item.status != target:
                allowed = {
                    "provisioning": {"active", "inactive", "revoked"},
                    "active": {"inactive", "revoked"},
                    "inactive": {"active", "revoked"},
                    "revoked": set(),
                }
                if target not in allowed[item.status]:
                    raise ConflictError(
                        "Physical device lifecycle transition is not allowed"
                    )
                if self.repository.structure_house("device", device_id) != house_id:
                    raise ConflictError("Physical device binding is inconsistent")
                self.repository.set_status(item, target)
                self._audit(device_id, house_id, actor_id, target)
            result = PhysicalDeviceRead.model_validate(item)
        return result

    def _audit(self, device_id: str, house_id: str, actor_id: str, status: str) -> None:
        self.repository.audit(
            {
                "id": str(uuid4()),
                "house_id": house_id,
                "event_type": "device_onboarding_changed",
                "entity_id": device_id,
                "payload": {
                    "device_id": device_id,
                    "actor_id": actor_id,
                    "status": status,
                },
                "correlation_id": str(uuid4()),
            }
        )


class PhysicalBindingGuard:
    """Protect permanent bindings against existing structure and device CRUD."""

    def __init__(self, repository: PhysicalDeviceRepository):
        self.repository = repository

    def structure_mutation(
        self,
        kind: str,
        entity_id: str,
        target_house: str | None = None,
        *,
        deleting: bool = False,
    ) -> None:
        source = self.repository.structure_house(kind, entity_id)
        self.repository.lock_houses(sorted({source, target_house or source}))
        if self.repository.structure_house(kind, entity_id) != source:
            raise ConflictError("Location changed concurrently; retry")
        if (
            deleting or target_house not in (None, source)
        ) and self.repository.bound_in_structure(kind, entity_id):
            raise ConflictError(
                "Physical device bindings cannot be deleted or transferred"
            )

    def require_active(self, device_id: str, *, lock: bool = False) -> None:
        # Do not hold a row lock across awaited transport/event delivery. State
        # and status persistence request a lock, then commit synchronously before
        # publishing events. Commands/ACK/telemetry use admission-time checks.
        item = self.repository.by_device(device_id, lock=lock)
        if item is None:
            return  # Existing virtual/E2E/legacy records are unchanged.
        if item.status != "active":
            raise ConflictError("Physical device is not active")
        if self.repository.structure_house("device", device_id) != item.house_id:
            raise ConflictError("Physical device binding is inconsistent")
