"""Real HTTP/RBAC with isolated SQLite; no external installation access."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import UserORM
from app.schemas.auth import HousePermissionsRead
from app.security import Permission, ROLE_PERMISSIONS
from app.services.auth_service import AuthorizationService
from test_command_lifecycle import fixture
from test_rbac import headers, rbac as rbac_fixture


@pytest.fixture
def rbac(tmp_path):
    yield from rbac_fixture.__wrapped__(tmp_path)


PATH = "/v1/houses/home1/me/permissions"
EXPECTED = {
    "owner": {
        "house.read",
        "house.manage",
        "member.read",
        "member.manage",
        "device.read",
        "device.control",
        "device.manage",
        "device.onboard",
        "scene.read",
        "scene.run",
        "scene.manage",
        "automation.read",
        "automation.manage",
        "event.read",
        "security.audit.read",
    },
    "installer": {
        "house.read",
        "device.read",
        "device.control",
        "device.manage",
        "device.onboard",
        "scene.read",
        "scene.run",
        "scene.manage",
        "automation.read",
        "automation.manage",
        "event.read",
    },
    "technician": {
        "house.read",
        "device.read",
        "device.control",
        "device.manage",
        "event.read",
    },
    "resident": {
        "house.read",
        "device.read",
        "device.control",
        "scene.read",
        "scene.run",
    },
}


@pytest.mark.parametrize("role", EXPECTED)
def test_current_user_exact_matrix_and_schema(rbac, role):
    client, _, _ = rbac
    response = client.get(PATH, headers=headers(client, role))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "house_id": "home1",
        "permissions": sorted(EXPECTED[role]),
    }
    assert HousePermissionsRead.model_validate(response.json()).house_id == "home1"
    assert len(response.json()["permissions"]) == len(
        set(response.json()["permissions"])
    )


def test_authentication_idor_and_superuser_has_no_bypass(rbac):
    client, settings, ids = rbac
    for auth in ({}, {"Authorization": "Bearer invalid"}):
        response = client.get(PATH, headers=auth)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
    owner = headers(client, "owner")
    for house in ("hidden-home", "missing-home"):
        response = client.get(f"/v1/houses/{house}/me/permissions", headers=owner)
        assert response.status_code == 404
        assert response.json() == {"detail": "Resource not found"}
    foreign = headers(client, "foreign")
    assert client.get(PATH, headers=foreign).status_code == 404
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            session.get(UserORM, ids["foreign"]).is_superuser = True
            session.commit()
        assert client.get(PATH, headers=foreign).status_code == 404
        with Session(engine) as session:
            session.get(UserORM, ids["owner"]).is_active = False
            session.commit()
        assert client.get(PATH, headers=owner).status_code == 401
    finally:
        engine.dispose()


def test_role_changes_revocation_and_query_cannot_select_another_user(rbac):
    client, _, ids = rbac
    owner, resident = headers(client, "owner"), headers(client, "resident")
    member_path = f"/houses/home1/members/{ids['resident']}"
    # Unrecognized query arguments never select a different principal/house.
    assert client.get(
        PATH + f"?user_id={ids['owner']}&house_id=hidden-home", headers=resident
    ).json()["permissions"] == sorted(EXPECTED["resident"])
    for role in ("installer", "technician", "resident"):
        assert (
            client.patch(member_path, headers=owner, json={"role": role}).status_code
            == 200
        )
        assert client.get(PATH, headers=resident).json()["permissions"] == sorted(
            EXPECTED[role]
        )
    assert client.delete(member_path, headers=owner).status_code == 204
    assert client.get(PATH, headers=resident).status_code == 404
    assert client.get(PATH, headers=owner).status_code == 200


def test_house_read_gate_uses_existing_policy(rbac, monkeypatch):
    client, _, _ = rbac
    resident = headers(client, "resident")
    monkeypatch.setitem(
        ROLE_PERMISSIONS, "resident", frozenset({Permission.DEVICE_READ})
    )
    response = client.get(PATH, headers=resident)
    assert response.status_code == 403
    assert response.json() == {"detail": "Insufficient house permission"}


def test_same_user_has_distinct_effective_permissions_in_each_house(rbac):
    client, _, ids = rbac
    owner, foreign = headers(client, "owner"), headers(client, "foreign")
    assert (
        client.post(
            "/houses/hidden-home/members",
            headers=foreign,
            json={
                "user_id": ids["owner"],
                "role": "resident",
            },
        ).status_code
        == 201
    )
    assert client.get(PATH, headers=owner).json()["permissions"] == sorted(
        EXPECTED["owner"]
    )
    assert client.get(
        "/v1/houses/hidden-home/me/permissions", headers=owner
    ).json() == {
        "house_id": "hidden-home",
        "permissions": sorted(EXPECTED["resident"]),
    }


@pytest.mark.parametrize("role", EXPECTED)
def test_command_authorization_remains_independent(tmp_path, monkeypatch, role):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            token = (
                await h.api.request(
                    "POST",
                    "/auth/login",
                    {
                        "email": "resident@example.test",
                        "password": "fixture-human-password",
                    },
                )
            )["access_token"]
            auth = {"Authorization": "Bearer " + token}
            user_id = (await h.api.client.get("/auth/me", headers=auth)).json()["id"]
            owner_auth = {"Authorization": "Bearer " + h.api.token}
            response = await h.api.client.patch(
                f"/houses/fixture_house/members/{user_id}",
                headers=owner_auth,
                json={"role": role},
            )
            assert response.status_code == 200
            path = "/v1/houses/fixture_house/me/permissions"
            response = await h.api.client.get(path, headers=auth)
            assert response.status_code == 200
            assert response.json()["permissions"] == sorted(EXPECTED[role])
            key = str(uuid4())
            accepted = await h.post(token=token, key=key)
            assert accepted.status_code == 202
            # Permissions are an observation, not a grant or a replay credential.
            monkeypatch.setitem(
                ROLE_PERMISSIONS,
                role,
                ROLE_PERMISSIONS[role] - {Permission.DEVICE_CONTROL},
            )
            permissions = (await h.api.client.get(path, headers=auth)).json()[
                "permissions"
            ]
            assert "device.control" not in permissions
            assert (await h.post(token=token, key=key)).status_code == 403
            assert (await h.post(token=token)).status_code == 403
            assert (await h.get(accepted.json(), token=token)).status_code == 200
            assert len(h.wire.published) == 1

    asyncio.run(check())


def test_schema_and_unknown_roles_fail_closed(rbac):
    client, _, _ = rbac
    for membership in (None, SimpleNamespace(role="unknown")):
        assert not any(
            AuthorizationService.has_permission(membership, p) for p in Permission
        )
    with pytest.raises(ValidationError):
        HousePermissionsRead(house_id="house", permissions=["invented.permission"])
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/houses/{house_id}/me/permissions"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/HousePermissionsRead")
    dto = schema["components"]["schemas"]["HousePermissionsRead"]
    assert set(dto["required"]) == {"house_id", "permissions"}
    assert set(dto["properties"]) == {"house_id", "permissions"}
