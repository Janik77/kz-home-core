"""Strict isolated control messages; never lifecycle ACK or legacy set."""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_CONTROL_FRAME = 4096
Hex128 = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PermitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    control_version: Literal[1]
    type: Literal["permit_request"]
    execution_profile: Literal["deadline-permit-v1"]
    core_epoch: str = Field(max_length=36)
    command_id: str = Field(max_length=36)
    boot_id: Hex128
    session_id: Hex128
    nonce: Hex128
    intent_sha256: Digest

    @field_validator("core_epoch", "command_id")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("Canonical UUID required")
        return value


def decode_request(payload: bytes) -> PermitRequest:
    if len(payload) > MAX_CONTROL_FRAME:
        raise ValueError("Oversized control frame")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate control field")
            result[key] = value
        return result

    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Invalid control JSON") from None
    # bool/float must not exploit equality to Literal[1].
    if not isinstance(raw, dict) or type(raw.get("control_version")) is not int:
        raise ValueError("Invalid control version")
    return PermitRequest.model_validate(raw)
