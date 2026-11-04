"""Durable API contract without network, claim, broker principals or permits."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    ProvisioningSessionORM,
    PhysicalDeviceORM,
    DeviceORM,
    DeviceCommandORM,
)
from app.services.provisioning_service import ProvisioningService
import test_onboarding

onboarding = test_onboarding.onboarding
claim_body = test_onboarding.claim_body

BASE = "/houses/a/provisioning-sessions"


def create(stack, key=None, role="installer", **patch):
    client, _, headers, code, *_ = stack
    return client.post(
        BASE,
        headers={**headers[role], "Idempotency-Key": str(key or uuid4())},
        json=claim_body(code, **patch),
    )


def cancel(stack, result, key=None, revision=0, role="installer"):
    client, _, headers, *_ = stack
    return client.post(
        BASE + "/" + result["session_id"] + "/cancel",
        headers={**headers[role], "Idempotency-Key": str(key or uuid4())},
        json={"expected_revision": revision},
    )


def test_create_read_cancel_idempotency_and_no_side_effects(onboarding):
    client, engine, headers, code, wire, app = onboarding
    key, cancel_key = uuid4(), uuid4()
    first = create(onboarding, key)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "discovered" and body["revision"] == 0
    assert first.headers["cache-control"] == "no-store"
    assert create(onboarding, key).json() == body
    assert create(onboarding, key, name="Different").status_code == 409
    assert create(onboarding).status_code == 409
    read = client.get(BASE + "/" + body["session_id"], headers=headers["owner"])
    assert read.json() == body and read.headers["cache-control"] == "no-store"
    done = cancel(onboarding, body, cancel_key, role="owner")
    assert done.status_code == 200 and done.json()["revision"] == 1
    assert done.json()["status"] == "cancelled"
    assert cancel(onboarding, body, cancel_key, role="owner").json() == done.json()
    assert (
        cancel(onboarding, body, cancel_key, revision=1, role="owner").status_code
        == 409
    )
    assert cancel(onboarding, body).status_code == 409
    assert create(onboarding, key).json() == done.json()
    assert create(onboarding).status_code == 201
    with Session(engine) as session:
        inventory = session.get(PhysicalDeviceORM, "physical1")
        assert inventory.status == "unprovisioned" and inventory.house_id is None
        assert inventory.claim_code_hash is not None
        assert session.scalar(select(func.count()).select_from(DeviceORM)) == 0
        assert session.scalar(select(func.count()).select_from(DeviceCommandORM)) == 0
        row = session.get(ProvisioningSessionORM, body["session_id"])
        assert len(row.challenge_hash) == len(row.token_hash) == 64
        assert row.challenge_hash != row.token_hash
        assert code not in repr(row.__dict__)
    assert wire.published == []
    with pytest.raises(Exception, match="Physical control remains closed"):
        app.state.control_authority.gate(None, "a", "physical1")


@pytest.mark.parametrize(
    "role,status",
    [("resident", 403), ("technician", 403), ("foreign", 404), ("outsider", 404)],
)
def test_rbac_for_all_operations(onboarding, role, status):
    client, _, headers, *_ = onboarding
    body = create(onboarding).json()
    assert create(onboarding, role=role).status_code == status
    assert (
        client.get(BASE + "/" + body["session_id"], headers=headers[role]).status_code
        == status
    )
    assert cancel(onboarding, body, role=role).status_code == status


def test_isolation_validation_and_no_secret_echo(onboarding, caplog):
    client, _, headers, code, *_ = onboarding
    body = create(onboarding).json()
    other = "/houses/b/provisioning-sessions/" + body["session_id"]
    assert client.get(other, headers=headers["both"]).status_code == 404
    assert (
        client.post(
            other + "/cancel",
            json={"expected_revision": 0},
            headers={**headers["both"], "Idempotency-Key": str(uuid4())},
        ).status_code
        == 404
    )
    for patch, expected in [
        ({"room_id": "rb"}, 404),
        ({"hardware_id": "unknown"}, 404),
        ({"claim_code": "b" * 43}, 404),
        ({"credentials": code}, 422),
        ({"claim_code": "secret"}, 422),
    ]:
        response = create(onboarding, **patch)
        assert response.status_code == expected
        assert code not in response.text and "secret" not in response.text
    assert code not in caplog.text
    assert client.post(BASE, json=claim_body(code)).status_code == 401
    assert (
        client.post(BASE, headers=headers["owner"], json=claim_body(code)).status_code
        == 422
    )
    assert client.get(BASE + "/not-a-uuid", headers=headers["owner"]).status_code == 422
    assert (
        client.get(BASE + "/" + str(uuid4()), headers=headers["owner"]).status_code
        == 404
    )
    assert (
        not {
            "challenge_hash",
            "token_hash",
            "claim_code",
            "create_key_hash",
            "name",
            "actor_id",
        }
        & body.keys()
    )


def test_expiry_retry_and_slot_reuse(onboarding, monkeypatch):
    _, engine, _, _, *_ = onboarding
    key = uuid4()
    initial = create(onboarding, key).json()
    with Session(engine) as session:
        row = session.get(ProvisioningSessionORM, initial["session_id"])
        from app.services.provisioning_service import utc

        deadline = utc(row.expires_at)
    monkeypatch.setattr(ProvisioningService, "_now", lambda self: deadline)
    # Cancel's conflict must not roll back materialized expiry.
    assert cancel(onboarding, initial).status_code == 409
    replay = create(onboarding, key).json()
    assert replay["status"] == "expired" and replay["revision"] == 1
    assert replay["expires_at"] == initial["expires_at"]
    assert create(onboarding).status_code == 201
    with Session(engine) as session:
        assert (
            session.get(ProvisioningSessionORM, initial["session_id"]).status
            == "expired"
        )


def test_get_expires_once_and_checks_current_permission(onboarding, monkeypatch):
    client, engine, headers, *_ = onboarding
    body = create(onboarding).json()
    from app.models import HouseMembershipORM
    from app.services.provisioning_service import utc

    with Session(engine) as session:
        deadline = utc(
            session.get(ProvisioningSessionORM, body["session_id"]).expires_at
        )
        session.execute(
            update(HouseMembershipORM)
            .where(HouseMembershipORM.user_id == "installer")
            .values(role="resident")
        )
        session.commit()
    monkeypatch.setattr(
        ProvisioningService, "_now", lambda self: deadline + timedelta(seconds=1)
    )
    assert (
        client.get(
            BASE + "/" + body["session_id"], headers=headers["installer"]
        ).status_code
        == 403
    )
    response = client.get(BASE + "/" + body["session_id"], headers=headers["owner"])
    assert response.json()["status"] == "expired" and response.json()["revision"] == 1
    assert (
        client.get(BASE + "/" + body["session_id"], headers=headers["owner"]).json()
        == response.json()
    )


def test_manual_claim_remains_compatible(onboarding):
    client, engine, headers, code, *_ = onboarding
    create(onboarding)
    result = client.post(
        "/houses/a/device-claims", headers=headers["owner"], json=claim_body(code)
    )
    assert result.status_code == 201
    assert create(onboarding).status_code == 404
    with Session(engine) as session:
        assert session.get(PhysicalDeviceORM, "physical1").status == "provisioning"


def test_stale_cancel_cannot_mutate_snapshot(onboarding):
    body = create(onboarding).json()
    assert cancel(onboarding, body, revision=1).status_code == 409
    assert cancel(onboarding, body, revision=0).status_code == 200


def test_storage_failures_are_redacted(onboarding, monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError

    secret = "private-driver-parameter"

    def unavailable(self):
        raise OperationalError("sensitive SQL", {"token": secret}, Exception(secret))

    monkeypatch.setattr(ProvisioningService, "_now", unavailable)
    response = create(onboarding)
    assert response.status_code == 503
    assert response.json() == {"detail": "Provisioning storage unavailable"}
    assert secret not in response.text and secret not in caplog.text


@pytest.mark.parametrize("identity", ["unknown", "wrong_code", "claimed"])
def test_claim_verification_always_compares_fixed_length_digests(
    onboarding, monkeypatch, identity
):
    from hmac import compare_digest

    client, _, headers, code, *_ = onboarding
    if identity == "claimed":
        assert (
            client.post(
                "/houses/a/device-claims",
                headers=headers["owner"],
                json=claim_body(code),
            ).status_code
            == 201
        )
    comparisons = []

    def record(expected, supplied):
        comparisons.append((len(expected), len(supplied)))
        return compare_digest(expected, supplied)

    monkeypatch.setattr("app.services.provisioning_service.compare_digest", record)
    patch = {"hardware_id": "unknown"} if identity == "unknown" else {}
    if identity == "wrong_code":
        patch["claim_code"] = "b" * 43
    response = create(onboarding, **patch)
    assert response.status_code == 404
    assert response.json() == {"detail": "Resource not found"}
    assert comparisons == [(64, 64)]
