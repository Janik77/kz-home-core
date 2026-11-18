"""Definition-only copies; no authority, run identity or scheduler state is copied."""

from copy import deepcopy
from pydantic import ValidationError
from sqlalchemy import select
from app.core.errors import (
    ConflictError,
    DefinitionRevisionConflict,
    InvalidReferenceError,
)
from app.models import SceneORM
from app.schemas import SceneCreate, SceneRead, AutomationCreate, AutomationRead
from app.services.wait_contract import wait_plan


def require_lossless(raw, parsed):
    """Defaults may be added, but no source field/value may be discarded/coerced."""
    if isinstance(raw, dict):
        if not isinstance(parsed, dict) or any(k not in parsed for k in raw):
            raise InvalidReferenceError(
                "Unsupported definition fields; original remains unchanged"
            )
        for k, value in raw.items():
            require_lossless(value, parsed[k])
    elif isinstance(raw, list):
        if not isinstance(parsed, list) or len(raw) != len(parsed):
            raise InvalidReferenceError("Unsupported definition plan")
        for a, b in zip(raw, parsed):
            require_lossless(a, b)
    elif type(raw) is not type(parsed) or raw != parsed:
        raise InvalidReferenceError("Definition cannot be copied losslessly")


def locked_definition(repository, entity_id, revision, house_id):
    row = repository.session.scalar(
        select(repository.model)
        .where(repository.model.id == entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.house_id != house_id or row.revision != revision:
        raise DefinitionRevisionConflict()
    return row


def static_scene_plan(service, house_id, actions):
    try:
        wait_plan(actions)
    except (ValueError, TypeError) as error:
        raise InvalidReferenceError(str(error)) from error
    if not 1 <= len(actions) <= 16:
        raise InvalidReferenceError("Scene activation requires 1 to 16 supported steps")
    for a in actions:
        if a.get("type") == "durable_wait":
            continue
        if (
            set(a) - {"type", "device_id", "state"}
            or a.get("type", "device_state") != "device_state"
            or not isinstance(a.get("device_id"), str)
            or not 1 <= len(a["device_id"]) <= 64
            or not isinstance(a.get("state"), dict)
            or set(a["state"]) != {"on"}
            or type(a["state"]["on"]) is not bool
        ):
            raise InvalidReferenceError(
                "Only supported Boolean relay actions can be activated"
            )
        device = service.devices.repository.get(a["device_id"])
        if (
            service.devices.repository.house_id(device.id) != house_id
            or device.type != "relay"
            or "on_off" not in device.capabilities
        ):
            raise InvalidReferenceError(
                "Scene action requires a same-house relay with on_off"
            )


def duplicate(service, source_id, request, house_id):
    repo = service.repository
    row = locked_definition(repo, source_id, request.expected_revision, house_id)
    if repo.exists(request.id):
        raise ConflictError(
            "Copy ID already exists; inspect it before choosing a new ID"
        )
    scene = isinstance(row, SceneORM)
    raw = {
        "id": request.id,
        "name": request.name,
        "house_id": house_id,
        "actions": deepcopy(row.actions),
    }
    if not scene:
        raw.update(
            enabled=False,
            trigger=deepcopy(row.trigger),
            conditions=deepcopy(row.conditions),
        )
    try:
        parsed = (SceneCreate if scene else AutomationCreate).model_validate(raw)
    except ValidationError as error:
        raise InvalidReferenceError(
            "Unsupported source definition; copy refused"
        ) from error
    require_lossless(raw, parsed.model_dump())
    # Inert copies may be edited offline; structural references remain valid.
    service._validate(parsed)
    values = parsed.model_dump()
    if scene:
        values["is_draft"] = True
    else:
        values.update(authorized_by=None, threshold_state={}, schedule_state={})
    copied = repo.create(values)
    return (SceneRead if scene else AutomationRead).model_validate(copied)


def activate(service, scene_id, request, house_id):
    row = locked_definition(
        service.repository, scene_id, request.expected_revision, house_id
    )
    if not row.is_draft:
        raise ConflictError("Scene is already active")
    static_scene_plan(service, house_id, row.actions)
    return SceneRead.model_validate(
        service.repository.update(
            scene_id,
            {"is_draft": False},
            expected_revision=request.expected_revision,
            expected_house_id=house_id,
        )
    )
