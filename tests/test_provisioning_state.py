"""Offline contract tests: never touch the database, broker or hardware."""

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.onboarding import DeviceClaim
from app.schemas.provisioning import (
    ProvisioningCancel,
    ProvisioningConfirm,
    ProvisioningErrorCode as Error,
    ProvisioningPublicStatus,
    ProvisioningStart,
    ProvisioningStatus as Status,
)
from app.services.provisioning_state import (
    ProvisioningConflict,
    ProvisioningEvent as Event,
    ProvisioningExpired,
    ProvisioningState,
    challenge_digest,
    transition,
)

NOW = datetime(2026, 11, 4, tzinfo=UTC)
CHALLENGE = "a" * 43  # Format fixture only; production must use a CSPRNG.


def attempt(status=Status.DISCOVERED):
    return ProvisioningState(
        session_id=UUID("11111111-1111-4111-8111-111111111111"),
        house_id="house",
        device_id="physical",
        room_id="room",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        challenge_hash=challenge_digest(CHALLENGE),
        status=status,
        proof_consumed=status
        in {
            Status.AWAITING_CONFIRMATION,
            Status.REGISTERED,
            Status.CONNECTED,
        },
        error_code=Error.PROOF_REJECTED if status == Status.ERROR else None,
    )


def step(state, event, **values):
    return transition(state, event, expected_revision=state.revision, now=NOW, **values)


ALLOWED = {
    (Status.DISCOVERED, Event.PROOF_VERIFIED): Status.AWAITING_CONFIRMATION,
    (Status.AWAITING_CONFIRMATION, Event.CONFIRM): Status.REGISTERED,
    (Status.REGISTERED, Event.CONNECTED): Status.CONNECTED,
    **{
        (status, event): target
        for status in (
            Status.DISCOVERED,
            Status.AWAITING_CONFIRMATION,
            Status.REGISTERED,
        )
        for event, target in (
            (Event.CANCEL, Status.CANCELLED),
            (Event.FAIL, Status.ERROR),
        )
    },
}


@pytest.mark.parametrize("status", list(Status))
@pytest.mark.parametrize("event", list(Event))
def test_transition_matrix(status, event):
    state = attempt(status)
    values = {}
    if event == Event.PROOF_VERIFIED:
        values["challenge"] = CHALLENGE
    if event == Event.FAIL:
        values["error_code"] = Error.PROOF_REJECTED
    target = ALLOWED.get((status, event))
    if target is None:
        with pytest.raises(ProvisioningConflict):
            step(state, event, **values)
    else:
        result = step(state, event, **values)
        assert result.status == target
        assert result.revision == state.revision + 1
        assert result.session_id == state.session_id
        assert state.status == status


def test_success_is_immutable_and_repeated_confirm_and_proof_are_rejected():
    initial = attempt()
    verified = step(initial, Event.PROOF_VERIFIED, challenge=CHALLENGE)
    assert verified.proof_consumed
    with pytest.raises(ProvisioningConflict):
        step(verified, Event.PROOF_VERIFIED, challenge=CHALLENGE)
    registered = step(verified, Event.CONFIRM)
    with pytest.raises(ProvisioningConflict):
        step(registered, Event.CONFIRM)
    connected = step(registered, Event.CONNECTED)
    assert connected.status == Status.CONNECTED
    assert connected.revision == 3
    assert not initial.proof_consumed
    with pytest.raises(FrozenInstanceError):
        initial.revision = 10


@pytest.mark.parametrize("challenge", [None, "b" * 43, "short", "!" * 43])
def test_wrong_or_missing_challenge_cannot_consume_proof(challenge):
    state = attempt()
    with pytest.raises(ValueError):
        step(state, Event.PROOF_VERIFIED, challenge=challenge)
    assert not state.proof_consumed
    assert state.revision == 0


@pytest.mark.parametrize("revision", [-1, 1, True, "0"])
def test_revision_conflict(revision):
    with pytest.raises(ProvisioningConflict, match="Revision conflict"):
        transition(attempt(), Event.CANCEL, expected_revision=revision, now=NOW)


def test_competing_snapshots_require_future_database_cas():
    initial = attempt()
    winner = step(initial, Event.PROOF_VERIFIED, challenge=CHALLENGE)
    with pytest.raises(ProvisioningConflict, match="Revision conflict"):
        transition(winner, Event.CANCEL, expected_revision=initial.revision, now=NOW)
    # The pure model deliberately cannot arbitrate separate process snapshots.
    assert step(initial, Event.CANCEL).status == Status.CANCELLED


@pytest.mark.parametrize(
    "status", [Status.DISCOVERED, Status.AWAITING_CONFIRMATION, Status.REGISTERED]
)
@pytest.mark.parametrize("offset", [0, 1])
def test_expiry_boundary_and_materialization(status, offset):
    state = attempt(status)
    now = state.expires_at + timedelta(seconds=offset)
    with pytest.raises(ProvisioningExpired):
        transition(state, Event.CANCEL, expected_revision=0, now=now)
    expired = transition(state, Event.EXPIRE, expected_revision=0, now=now)
    assert expired.status == Status.EXPIRED
    assert expired.revision == 1
    assert state.status == status
    with pytest.raises(ProvisioningConflict):
        transition(expired, Event.EXPIRE, expected_revision=1, now=now)


def test_confirm_just_before_expiry_and_replay_at_deadline():
    state = attempt(Status.AWAITING_CONFIRMATION)
    result = transition(
        state,
        Event.CONFIRM,
        expected_revision=0,
        now=state.expires_at - timedelta(microseconds=1),
    )
    assert result.status == Status.REGISTERED
    with pytest.raises(ProvisioningExpired):
        transition(state, Event.CONFIRM, expected_revision=0, now=state.expires_at)


def test_challenge_from_another_attempt_is_rejected():
    other = replace(attempt(), challenge_hash=challenge_digest("b" * 43))
    with pytest.raises(ProvisioningConflict, match="Challenge mismatch"):
        step(other, Event.PROOF_VERIFIED, challenge=CHALLENGE)


def test_failure_requires_allowlisted_code_and_unknown_events_are_rejected():
    with pytest.raises(ProvisioningConflict):
        step(attempt(), Event.FAIL)
    with pytest.raises(ProvisioningConflict):
        step(attempt(), Event.FAIL, error_code="raw exception with secrets")
    with pytest.raises(ProvisioningConflict, match="Unknown event"):
        step(attempt(), "confirm")


def test_terminal_success_is_not_expired_later():
    state = attempt(Status.CONNECTED)
    with pytest.raises(ProvisioningConflict, match="terminal"):
        transition(
            state,
            Event.EXPIRE,
            expected_revision=0,
            now=state.expires_at + timedelta(days=1),
        )
    assert state.status == Status.CONNECTED


def test_discovered_snapshot_cannot_have_consumed_proof():
    with pytest.raises(ValueError):
        replace(attempt(), proof_consumed=True)


@pytest.mark.parametrize(
    "values",
    [
        {"challenge": CHALLENGE},
        {"error_code": Error.PROOF_REJECTED},
    ],
)
def test_unexpected_event_fields(values):
    with pytest.raises(ProvisioningConflict):
        step(attempt(), Event.CANCEL, **values)


@pytest.mark.parametrize("now", [NOW.replace(tzinfo=None), NOW - timedelta(seconds=1)])
def test_invalid_clock_input(now):
    with pytest.raises(ValueError):
        transition(attempt(), Event.CANCEL, expected_revision=0, now=now)


@pytest.mark.parametrize(
    "values",
    [
        {"expires_at": NOW},
        {"created_at": NOW.replace(tzinfo=None)},
        {"challenge_hash": "bad"},
        {"revision": -1},
        {"revision": True},
        {"device_id": "bad/+"},
        {"status": Status.REGISTERED},
        {"error_code": Error.PROOF_REJECTED},
        {"proof_consumed": 1},
    ],
)
def test_invalid_snapshots(values):
    with pytest.raises(ValueError):
        replace(attempt(), **values)


def test_public_projection_has_only_allowlisted_fields():
    public = attempt().public_status()
    assert set(public.model_dump()) == {
        "session_id",
        "house_id",
        "device_id",
        "room_id",
        "status",
        "revision",
        "expires_at",
        "error_code",
    }
    serialized = public.model_dump_json()
    assert CHALLENGE not in serialized
    assert attempt().challenge_hash not in serialized
    assert attempt().challenge_hash not in repr(attempt())
    assert ProvisioningPublicStatus.model_validate_json(serialized) == public


@pytest.mark.parametrize(
    "field",
    [
        "claim_code",
        "challenge",
        "challenge_hash",
        "credentials",
        "mqtt_password",
        "enrollment_token",
        "proof_verified",
        "physical_gate",
    ],
)
def test_public_status_forbids_sensitive_or_authority_fields(field):
    with pytest.raises(ValidationError):
        ProvisioningPublicStatus(
            **attempt().public_status().model_dump(), **{field: "secret"}
        )


def test_request_reuses_claim_contract_and_redacts_secret():
    assert issubclass(ProvisioningStart, DeviceClaim)
    request = ProvisioningStart(
        hardware_id="serial", claim_code="z" * 43, room_id="room", name="Relay"
    )
    assert "z" * 43 not in repr(request)
    assert "z" * 43 not in request.model_dump_json()
    with pytest.raises(ValidationError):
        ProvisioningStart(**request.model_dump(), credentials="secret")


@pytest.mark.parametrize("schema", [ProvisioningConfirm, ProvisioningCancel])
def test_public_mutations_only_accept_strict_revision(schema):
    assert schema(expected_revision=0).expected_revision == 0
    for revision in (-1, True, "0"):
        with pytest.raises(ValidationError):
            schema(expected_revision=revision)
    with pytest.raises(ValidationError):
        schema(expected_revision=0, proof_verified=True)


def test_new_modules_have_no_operational_dependencies():
    root = Path(__file__).resolve().parents[1]
    allowed = {
        "__future__",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "hmac",
        "re",
        "typing",
        "uuid",
        "pydantic",
    }
    allowed_app = {"app.schemas.onboarding", "app.schemas.provisioning"}
    for relative in (
        "app/schemas/provisioning.py",
        "app/services/provisioning_state.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name in allowed for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module in allowed | allowed_app
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "exec", "eval", "__import__"}
