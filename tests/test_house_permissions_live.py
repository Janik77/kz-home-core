"""Opt-in actual HTTP/PostgreSQL in an exclusively owned Docker fixture."""

import asyncio
import hashlib
import json
import os
from uuid import uuid4

import httpx
import pytest

from test_house_permissions import EXPECTED
from test_physical_acceptance import provision
from test_physical_acceptance_live import command, ready, stack

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_HOUSE_PERMISSIONS_DOCKER_TESTS") != "1",
    reason="RUN_HOUSE_PERMISSIONS_DOCKER_TESTS=1 required; isolated stack only",
)


def inventory():
    ids = command(["docker", "ps", "-aq"]).splitlines()
    containers = {}
    for item in json.loads(command(["docker", "inspect", *ids])) if ids else []:
        config = {key: item[key] for key in ("Image", "Config", "Mounts", "HostConfig")}
        containers[item["Id"]] = (
            item["State"]["Status"],
            item["State"]["StartedAt"],
            item["RestartCount"],
            hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        )
    return {
        "containers": containers,
        "volumes": sorted(command(["docker", "volume", "ls", "-q"]).splitlines()),
        "networks": sorted(
            command(
                ["docker", "network", "ls", "--format", "{{.ID}} {{.Name}}"]
            ).splitlines()
        ),
        "images": sorted(
            command(
                [
                    "docker",
                    "image",
                    "ls",
                    "--no-trunc",
                    "--format",
                    "{{.ID}} {{.Repository}}:{{.Tag}}",
                ]
            ).splitlines()
        ),
    }


def test_permissions_http_postgresql_and_installed_stack_preservation(tmp_path):
    before = inventory()
    owned = stack.__wrapped__(tmp_path)
    try:
        dc, _, _, seed, ids, api = next(owned)

        def sql(query):
            return command(
                [
                    *dc,
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "kzhome",
                    "-At",
                    "-c",
                    query,
                ]
            )

        async def check():
            await ready(api)
            await provision(api, ids, seed)
            assert (
                sql("SELECT version_num FROM alembic_version")
                == "0006_command_lifecycle"
            )
            async with httpx.AsyncClient(base_url=api.base, timeout=10) as http:

                async def login(role):
                    response = await http.post(
                        "/auth/login",
                        json={
                            "email": f"{role}@example.test",
                            "password": seed["password"],
                        },
                    )
                    assert response.status_code == 200
                    return {
                        "Authorization": "Bearer " + response.json()["access_token"]
                    }

                owner = await login("owner")
                resident = await login("resident")
                foreign = await login("foreign")
                path = "/v1/houses/fixture_house/me/permissions"
                assert (await http.get(path)).status_code == 401
                assert (await http.get(path, headers=foreign)).status_code == 404
                assert (
                    await http.get("/v1/houses/absent/me/permissions", headers=owner)
                ).status_code == 404
                assert (await http.get(path, headers=owner)).json() == {
                    "house_id": "fixture_house",
                    "permissions": sorted(EXPECTED["owner"]),
                }
                member_path = f"/houses/fixture_house/members/{ids['resident']}"
                for role in ("resident", "installer", "technician"):
                    assert (
                        await http.patch(
                            member_path, headers=owner, json={"role": role}
                        )
                    ).status_code == 200
                    result = await http.get(path, headers=resident)
                    assert result.status_code == 200
                    assert result.headers["cache-control"] == "no-store"
                    assert result.json()["permissions"] == sorted(EXPECTED[role])
                    # Physical relay is offline: authorized does not mean eligible.
                    result = await http.post(
                        "/v1/devices/fixture_relay/commands",
                        headers={**resident, "Idempotency-Key": str(uuid4())},
                        json={"on": True},
                    )
                    assert result.status_code == 409
                    assert (
                        await http.get(
                            "/houses/fixture_house/members", headers=resident
                        )
                    ).status_code == 403
                assert (
                    await http.delete(member_path, headers=owner)
                ).status_code == 204
                assert (await http.get(path, headers=resident)).status_code == 404
                assert (
                    await http.post(
                        "/v1/devices/fixture_relay/commands",
                        headers={**resident, "Idempotency-Key": str(uuid4())},
                        json={"on": True},
                    )
                ).status_code == 404
                assert (await http.get(path, headers=owner)).status_code == 200
                assert sql("SELECT count(*) FROM device_commands") == "0"
                assert (
                    sql("SELECT count(*) FROM house_memberships WHERE role='owner'")
                    == "1"
                )
                # Deactivate only the independently provisioned fixture owner.
                sql("UPDATE users SET is_active=false WHERE email='owner@example.test'")
                assert (await http.get(path, headers=owner)).status_code == 401

        asyncio.run(check())
    finally:
        owned.close()
        assert inventory() == before, (
            "Existing Docker resources changed; inspect before cleanup"
        )
