"""Real API/RBAC, migrated SQLite and MQTT gateway; network alone is fake."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import logging
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core import Settings
from app.models import DeviceCommandORM, EventLogORM
from app.transports.mqtt_client import FakeMQTTClient, MQTTMessage
from test_physical_acceptance import stack


class Harness:
    def __init__(self, api, wire, engine):
        self.api, self.wire, self.engine = api, wire, engine
        self.gateway = api.client._transport.app.state.mqtt_gateway

    async def post(
        self, on=True, key=None, device="fixture_relay", token=None, body=None
    ):
        return await self.api.client.post(
            f"/v1/devices/{device}/commands",
            json={"on": on} if body is None else body,
            headers={
                "Authorization": "Bearer " + (token or self.api.token),
                "Idempotency-Key": key or str(uuid4()),
            },
        )

    async def get(self, receipt, house="fixture_house", token=None):
        response = await self.api.client.get(
            f"/v1/houses/{house}/commands/{receipt['command_id']}",
            headers={"Authorization": "Bearer " + (token or self.api.token)},
        )
        return response

    async def message(
        self,
        kind,
        payload,
        retained=False,
        house="fixture_house",
        device="fixture_relay",
    ):
        await self.gateway.handle_message(
            MQTTMessage(
                f"kzhome/v1/{house}/{device}/{kind}",
                json.dumps(payload).encode(),
                retained,
            )
        )

    async def ack(self, receipt, status="applied", **changes):
        retained = changes.pop("retained", False)
        await self.message(
            "ack",
            {
                "command_id": receipt["command_id"],
                "correlation_id": receipt["correlation_id"],
                "status": status,
                **changes,
            },
            retained,
        )

    async def state(self, receipt, on, retained=False):
        await self.message(
            "state",
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "correlation_id": receipt["correlation_id"],
                "state": {"on": on},
            },
            retained,
        )

    def expire(self, receipt):
        with Session(self.engine) as session:
            row = session.get(DeviceCommandORM, receipt["command_id"])
            row.created_at = datetime.now(UTC) - timedelta(seconds=60)
            row.expires_at = datetime.now(UTC) - timedelta(seconds=30)
            session.commit()


@asynccontextmanager
async def fixture(tmp_path, monkeypatch):
    # The reused migration helper uses Alembic fileConfig, which disables existing
    # loggers. Do not leak that test-only side effect into later caplog tests.
    loggers = {
        value: value.disabled
        for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger)
    }
    try:
        async with stack(tmp_path, monkeypatch) as (api, wire, _, _, engine):
            h = Harness(api, wire, engine)
            await h.message(
                "status",
                {"status": "online", "last_seen": datetime.now(UTC).isoformat()},
            )
            await h.state({"correlation_id": "unsolicited-baseline"}, False)
            yield h
    finally:
        for logger, disabled in loggers.items():
            logger.disabled = disabled


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("state_first", [True, False])
def test_applied_requires_exact_ack_and_live_correlated_state_even_if_unchanged(
    tmp_path, monkeypatch, on, state_first
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response = await h.post(on)
            assert response.status_code == 202
            receipt = response.json()
            assert receipt["status"] == "dispatched"
            assert receipt["ack_status"] is None and receipt["reported_on"] is None
            topic, payload, qos, retained = h.wire.published[-1]
            command = json.loads(payload)
            assert command["command_id"] == receipt["command_id"]
            assert command["correlation_id"] == receipt["correlation_id"]
            assert command["state"] == {"on": on} and (qos, retained) == (1, False)
            with Session(h.engine) as session:
                before = (
                    session.query(EventLogORM)
                    .filter_by(event_type="device_state_changed")
                    .count()
                )
            if state_first:
                await h.state(receipt, on)
            else:
                await h.ack(receipt)
            assert (await h.get(receipt)).json()["status"] == "dispatched"
            if state_first:
                await h.ack(receipt)
            else:
                await h.state(receipt, on)
            result = (await h.get(receipt)).json()
            assert result["status"] == "applied" and result["reported_on"] is on
            assert result["ack_status"] == "applied" and result["completed_at"]
            assert result["failure_code"] is None
            assert "requested_by" not in result and "idempotency_hash" not in result
            with Session(h.engine) as session:
                count = (
                    session.query(EventLogORM)
                    .filter_by(event_type="device_state_changed")
                    .count()
                )
                assert count == before + int(
                    on
                )  # Unchanged OFF emits no old state event.

    asyncio.run(check())


def test_stale_foreign_unknown_retained_and_accepted_evidence_cannot_confirm(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            receipt = (await h.post()).json()
            await h.state({"correlation_id": "old-or-foreign"}, True)
            await h.ack(receipt, command_id="unknown")
            await h.ack(receipt, correlation_id="different")
            await h.ack(receipt, retained=True)
            await h.state(receipt, True, retained=True)
            result = (await h.get(receipt)).json()
            assert result["ack_status"] is None and result["reported_on"] is None
            await h.ack(receipt, "accepted")
            await h.state(receipt, True)
            assert (await h.get(receipt)).json()["status"] == "dispatched"
            await h.ack(receipt)
            applied = (await h.get(receipt)).json()
            assert applied["status"] == "applied"
            await h.ack(receipt, "accepted")
            await h.ack(receipt, "rejected", message="private diagnostic")
            await h.ack(receipt)
            await h.state(receipt, False)
            assert (
                await h.get(receipt)
            ).json() == applied  # Terminal record never regresses.

    asyncio.run(check())


@pytest.mark.parametrize(
    "status,code", [("rejected", "device_rejected"), ("failed", "device_failed")]
)
def test_negative_ack_is_terminal_and_late_success_is_ignored(
    tmp_path, monkeypatch, status, code
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            receipt = (await h.post()).json()
            await h.state(receipt, True)
            await h.ack(
                receipt,
                status,
                error_code="secret-looking-input",
                message="not for HTTP",
            )
            result = (await h.get(receipt)).json()
            assert result["status"] == "failed" and result["failure_code"] == code
            assert "secret-looking-input" not in json.dumps(
                result
            ) and "not for HTTP" not in json.dumps(result)
            await h.ack(receipt)
            await h.state(receipt, True)
            assert (await h.get(receipt)).json() == result

    asyncio.run(check())


def test_wrong_state_and_applied_ack_wait_for_timeout_and_ignore_late_evidence(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            receipt = (await h.post()).json()
            await h.ack(receipt)
            await h.state(receipt, False)
            assert (await h.get(receipt)).json()["status"] == "dispatched"
            h.expire(receipt)
            result = (await h.get(receipt)).json()
            assert (
                result["status"] == "timed_out"
                and result["failure_code"] == "deadline_exceeded"
            )
            await h.state(receipt, True)
            await h.ack(receipt)
            assert (await h.get(receipt)).json() == result

    asyncio.run(check())


def test_idempotency_and_conflicting_intents_publish_once(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            key = str(uuid4())
            a, b = await asyncio.gather(h.post(True, key), h.post(True, key))
            assert a.status_code == b.status_code == 202
            assert a.json()["command_id"] == b.json()["command_id"]
            assert len(h.wire.published) == 1
            assert (await h.post(False, key)).status_code == 409
            assert (await h.post(False)).status_code == 409
            await h.ack(a.json())
            await h.state(a.json(), True)
            assert (await h.post(True, key)).json()["status"] == "applied"
            assert len(h.wire.published) == 1
            assert (await h.post(False)).status_code == 202
            assert len(h.wire.published) == 2

    asyncio.run(check())


def test_offline_and_broker_unavailable_are_rejected_without_dispatch(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            await h.message(
                "status",
                {"status": "offline", "last_seen": datetime.now(UTC).isoformat()},
            )
            assert (await h.post()).status_code == 409
            await h.message(
                "status",
                {"status": "online", "last_seen": datetime.now(UTC).isoformat()},
            )
            h.gateway.connected = False
            assert (await h.post()).status_code == 503
            assert not h.wire.published
            with Session(h.engine) as session:
                assert session.query(DeviceCommandORM).count() == 0

    asyncio.run(check())


def test_publish_failure_is_durable_unknown_outcome_not_applied(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:

            async def fail(*args, **kwargs):
                raise ConnectionError("credentials must not enter HTTP")

            h.gateway.client.publish = fail
            key = str(uuid4())
            response = await h.post(True, key)
            assert response.status_code == 202
            receipt = response.json()
            assert (
                receipt["status"] == "failed"
                and receipt["failure_code"] == "publish_unconfirmed"
            )
            assert "credentials" not in response.text
            await h.ack(receipt)
            await h.state(receipt, True)
            assert (await h.get(receipt)).json() == receipt
            assert (await h.post(True, key)).json() == receipt

    asyncio.run(check())


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"on": 1},
        {"on": "true"},
        {"on": None},
        {"on": True, "brightness": 20},
        {"toggle": True},
    ],
)
def test_invalid_intents_are_not_published(tmp_path, monkeypatch, body):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            assert (await h.post(body=body)).status_code == 422
            assert not h.wire.published

    asyncio.run(check())


def test_resident_result_read_is_not_audit_access_or_control_authorization(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            resident = (
                await h.api.request(
                    "POST",
                    "/auth/login",
                    {
                        "email": "resident@example.test",
                        "password": "fixture-human-password",
                    },
                )
            )["access_token"]
            receipt = (await h.post(token=resident)).json()
            assert (await h.get(receipt, token=resident)).status_code == 200
            r = await h.api.client.get(
                "/events?house_id=fixture_house",
                headers={"Authorization": "Bearer " + resident},
            )
            assert r.status_code == 403
            from app.security import ROLE_PERMISSIONS, Permission

            monkeypatch.setitem(
                ROLE_PERMISSIONS,
                "resident",
                ROLE_PERMISSIONS["resident"] - {Permission.DEVICE_CONTROL},
            )
            assert (await h.get(receipt, token=resident)).status_code == 200
            assert (await h.post(False, token=resident)).status_code == 403
            # Foreign and nonexistent command IDs have the same 404 boundary.
            foreign = (
                await h.api.request(
                    "POST",
                    "/auth/login",
                    {
                        "email": "foreign@example.test",
                        "password": "fixture-human-password",
                    },
                )
            )["access_token"]
            assert (await h.get(receipt, token=foreign)).status_code == 404
            assert (await h.post(token=foreign)).status_code == 404
            assert (await h.get({"command_id": str(uuid4())})).status_code == 404
            assert (await h.get(receipt, house="other-house")).status_code == 404
            response = await h.api.client.post(
                "/houses",
                json={"id": "foreign_house", "name": "Foreign"},
                headers={"Authorization": "Bearer " + foreign},
            )
            assert response.status_code == 201
            assert (
                await h.get(receipt, house="foreign_house", token=foreign)
            ).status_code == 404
            assert (
                await h.api.client.post(
                    "/v1/devices/fixture_relay/commands",
                    json={"on": True},
                    headers={"Idempotency-Key": str(uuid4())},
                )
            ).status_code == 401
            assert len(h.wire.published) == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    "device_type,capabilities",
    [("light", ["on_off"]), ("curtain", ["position"]), ("relay", [])],
)
def test_nonbinary_or_unsupported_devices_cannot_use_new_api(
    tmp_path, monkeypatch, device_type, capabilities
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            await h.api.request(
                "POST",
                "/devices",
                {
                    "id": "unsupported",
                    "room_id": "fixture_room",
                    "name": "Unsupported",
                    "type": device_type,
                    "capabilities": capabilities,
                },
            )
            assert (await h.post(device="unsupported")).status_code == 422
            assert not h.wire.published

    asyncio.run(check())


def test_cancelled_dispatch_survives_as_queued_and_retry_does_not_publish_again(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            entered = asyncio.Event()

            async def blocked(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(h.gateway, "send_command", blocked)
            key = str(uuid4())
            request = asyncio.create_task(h.post(True, key))
            await asyncio.wait_for(entered.wait(), 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            result = (await h.post(True, key)).json()
            assert result["status"] == "queued" and not h.wire.published
            h.expire(result)
            assert (await h.get(result)).json()["status"] == "timed_out"
            assert not h.wire.published

    asyncio.run(check())


def test_publish_deadline_is_bounded_and_not_retried(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:

            async def blocked(*args, **kwargs):
                await asyncio.Event().wait()

            monkeypatch.setattr(h.gateway, "send_command", blocked)
            monkeypatch.setattr(
                "app.services.command_service.PUBLISH_TIMEOUT_SECONDS", 0.01
            )
            result = (await h.post()).json()
            assert (
                result["status"] == "failed"
                and result["failure_code"] == "publish_unconfirmed"
            )
            assert not h.wire.published

    asyncio.run(check())


def test_restart_retains_pending_evidence_and_does_not_republish(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            receipt = (await h.post()).json()
            await h.ack(receipt)
            url, token = str(h.engine.url), h.api.token
        # Entire original app/gateway/session pool has stopped. Use the same owned DB.
        from app.main import create_app

        app = create_app(
            Settings(
                database_url=url,
                app_env="test",
                mqtt_enabled=True,
                mqtt_host="unused",
                mqtt_client_id="restarted",
            ),
            run_simulator=False,
            mqtt_client=FakeMQTTClient(),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                path = f"/v1/houses/fixture_house/commands/{receipt['command_id']}"
                result = await client.get(
                    path, headers={"Authorization": "Bearer " + token}
                )
                assert (
                    result.json()["status"] == "dispatched"
                    and result.json()["ack_status"] == "applied"
                )
                assert not app.state.mqtt_gateway.client.published
                await app.state.mqtt_gateway.handle_message(
                    MQTTMessage(
                        "kzhome/v1/fixture_house/fixture_relay/state",
                        json.dumps(
                            {
                                "timestamp": datetime.now(UTC).isoformat(),
                                "correlation_id": receipt["correlation_id"],
                                "state": {"on": True},
                            }
                        ).encode(),
                    )
                )
                assert (
                    await client.get(path, headers={"Authorization": "Bearer " + token})
                ).json()["status"] == "applied"

    asyncio.run(check())
