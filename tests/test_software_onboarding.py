"""Test only the isolated, explicitly configured software enrollment boundary."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core import Settings
from app.core.errors import InvalidReferenceError
from app.db import Base
from app.models import (
    DeviceORM,
    EventLogORM,
    FloorORM,
    HouseMembershipORM,
    HouseORM,
    RoomORM,
    UserORM,
)
from app.security import TokenCodec
from app.transports import FakeMQTTClient
from app.transports.mqtt_client import MQTTMessage

PATH = "/v1/houses/a/software-device-candidates"
BODY = {"room_id": "ra", "name": "Hall relay"}
CLAIM = PATH + "/fixture-relay/claim"
CANDIDATES = (
    ("a", "fixture-relay", "relay"),
    ("a", "fixture-temp", "temperature_sensor"),
)


def config(db, **overrides):
    values = dict(
        database_url=db,
        app_env="test",
        mqtt_enabled=True,
        mqtt_tls_enabled=True,
        mqtt_host="offline",
        mqtt_client_id="test-core",
        mqtt_software_devices=(
            ("a", "fixture-relay"),
            ("a", "fixture-temp"),
            ("b", "fixture-relay"),
        ),
        software_device_candidates=CANDIDATES,
    )
    return Settings(**(values | overrides))


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    from app.main import create_app

    settings = config(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    codec = TokenCodec(settings.auth_jwt_secret, "HS256", 15, 30)
    headers = {}
    with Session(engine) as session:
        session.add_all([HouseORM(id="a", name="A"), HouseORM(id="b", name="B")])
        session.flush()
        session.add_all(
            [
                FloorORM(id="fa", house_id="a", name="A", order=0),
                FloorORM(id="fb", house_id="b", name="B", order=0),
            ]
        )
        session.flush()
        session.add_all(
            [
                RoomORM(id="ra", floor_id="fa", name="A"),
                RoomORM(id="ra2", floor_id="fa", name="A2"),
                RoomORM(id="rb", floor_id="fb", name="B"),
            ]
        )
        for user, role, houses in [
            ("owner", "owner", ("a", "b")),
            ("foreigner", "owner", ("b",)),
            ("installer", "installer", ("a",)),
            ("technician", "technician", ("a",)),
            ("resident", "resident", ("a",)),
        ]:
            session.add(
                UserORM(id=user, email=f"{user}@example.test", password_hash="unused")
            )
            session.flush()
            for house in houses:
                session.add(
                    HouseMembershipORM(
                        id=f"{user}-{house}", user_id=user, house_id=house, role=role
                    )
                )
            token, _ = codec.issue(user, "access")
            headers[user] = {"Authorization": f"Bearer {token}"}
        session.commit()
    engine.dispose()

    def open_app(**changes):
        return TestClient(
            create_app(
                config(settings.database_url, **changes),
                run_simulator=False,
                mqtt_client=FakeMQTTClient(),
            )
        )

    return open_app, engine, headers


def test_flag_requires_exact_tls_lab_allowlist():
    for patch in (
        {"app_env": "production"},
        {"mqtt_enabled": False},
        {"mqtt_tls_enabled": False},
        {"mqtt_software_devices": ()},
        {"software_device_candidates": CANDIDATES + (("b", "fixture-relay", "relay"),)},
        {"software_device_candidates": (("a", "fixture-relay", "browser_defined"),)},
    ):
        with pytest.raises(ValueError):
            config("sqlite:///:memory:", **patch)


def test_hidden_or_unauthorized_candidates_and_bad_scope(lab):
    open_app, _, h = lab
    with open_app(software_device_candidates=()) as client:
        assert client.get(PATH, headers=h["owner"]).status_code == 404
    with open_app() as client:
        assert client.get(PATH).status_code == 401
        assert client.get(PATH, headers=h["foreigner"]).status_code == 404
        for role in ("technician", "resident"):
            assert client.get(PATH, headers=h[role]).status_code == 403
            assert client.post(CLAIM, json=BODY, headers=h[role]).status_code == 403
        assert client.get(PATH.replace("/a/", "/b/"), headers=h["owner"]).json() == []
        assert (
            client.post(
                CLAIM.replace("/a/", "/b/"),
                json={**BODY, "room_id": "rb"},
                headers=h["owner"],
            ).status_code
            == 404
        )
        assert (
            client.post(
                CLAIM, json={**BODY, "room_id": "rb"}, headers=h["owner"]
            ).status_code
            == 404
        )
        invalid = client.post(
            CLAIM, json={**BODY, "secret": "DO_NOT_ECHO"}, headers=h["owner"]
        )
        assert invalid.status_code == 422 and "DO_NOT_ECHO" not in invalid.text
        assert (
            client.post(
                CLAIM, json={**BODY, "name": "   "}, headers=h["owner"]
            ).status_code
            == 422
        )


def test_enrollment_no_fabricated_state_and_duplicate_survives_restart(lab):
    open_app, engine, h = lab
    with open_app() as client:
        list_res = client.get(PATH, headers=h["installer"])
        assert list_res.headers["cache-control"] == "no-store"
        assert list_res.json() == [
            {"device_id": "fixture-relay", "device_type": "relay"},
            {"device_id": "fixture-temp", "device_type": "temperature_sensor"},
        ]
        legacy = {
            **BODY,
            "id": "fixture-relay",
            "type": "relay",
            "capabilities": ["on_off"],
        }
        assert (
            client.post("/devices", json=legacy, headers=h["technician"]).status_code
            == 409
        )
        first = client.post(CLAIM, json=BODY, headers=h["installer"])
        assert first.status_code == 201
        relay = first.json()
        assert (
            relay["id"],
            relay["room_id"],
            relay["online"],
            relay["state"],
            relay["capabilities"],
        ) == ("fixture-relay", "ra", False, {}, ["on_off"])
        assert (
            client.post(
                CLAIM, json={**BODY, "name": "Different"}, headers=h["owner"]
            ).status_code
            == 409
        )
        sensor = client.post(
            PATH + "/fixture-temp/claim",
            json={"room_id": "ra", "name": "Temp"},
            headers=h["installer"],
        ).json()
        assert (
            sensor["capabilities"] == ["temperature"]
            and sensor["state"] == {}
            and not sensor["online"]
        )
        assert client.get(PATH, headers=h["owner"]).json() == []
        assert (
            client.get("/devices/fixture-relay", headers=h["foreigner"]).status_code
            == 404
        )
    with Session(engine) as session:
        # Exercise the mutable last_seen field written by the normal transport path.
        row = session.get(DeviceORM, "fixture-relay")
        row.metadata_ = {**row.metadata_, "last_seen": datetime.now(UTC).isoformat()}
        session.commit()
    with open_app() as client:
        repeat = client.post(CLAIM, json=BODY, headers=h["installer"])
        assert repeat.status_code == 201 and repeat.json()["id"] == relay["id"]
        assert "last_seen" in repeat.json()["metadata"]
        devices = client.get("/devices?house_id=a", headers=h["owner"]).json()
        assert {d["id"] for d in devices} == {"fixture-relay", "fixture-temp"}
    with Session(engine) as session:
        count = session.scalar(
            select(func.count(EventLogORM.id)).where(
                EventLogORM.event_type == "software_device_enrolled"
            )
        )
        assert count == 2


def test_identity_and_parent_changes_are_guarded(lab):
    open_app, _, h = lab
    with open_app() as client:
        owner = h["owner"]
        device = client.post(CLAIM, json=BODY, headers=owner).json()
        path = "/devices/fixture-relay"
        rev = {"expected_metadata_revision": device["metadata_revision"]}
        assert (
            client.patch(path, json={**rev, "online": True}, headers=owner).status_code
            == 409
        )
        assert (
            client.patch(path, json={**rev, "room_id": "rb"}, headers=owner).status_code
            == 409
        )
        assert (
            client.patch(
                "/rooms/ra", json={**rev, "floor_id": "fb"}, headers=owner
            ).status_code
            == 409
        )
        assert (
            client.patch(
                "/floors/fa", json={**rev, "house_id": "b"}, headers=owner
            ).status_code
            == 409
        )
        assert client.delete("/houses/a", headers=owner).status_code == 409
        assert client.delete("/rooms/ra", headers=owner).status_code == 409
        assert client.delete("/floors/fa", headers=owner).status_code == 409
        assert client.delete(path, headers=owner).status_code == 409
        result = client.patch(path, json={**rev, "room_id": "ra2"}, headers=owner)
        assert result.status_code == 200 and result.json()["room_id"] == "ra2"
        assert client.get("/devices?house_id=b", headers=owner).json() == []


def test_enrolled_capabilities_consume_scoped_reports_and_confirm_relay_command(lab):
    open_app, _, h = lab
    with open_app() as client:
        owner = h["owner"]
        assert client.post(CLAIM, json=BODY, headers=owner).status_code == 201
        assert (
            client.post(
                PATH + "/fixture-temp/claim",
                json={"room_id": "ra", "name": "Sensor"},
                headers=owner,
            ).status_code
            == 201
        )
        gateway = client.app.state.mqtt_gateway

        def report(house, device, kind, payload):
            client.portal.call(
                gateway.handle_message,
                MQTTMessage(
                    f"kzhome/v1/{house}/{device}/{kind}", json.dumps(payload).encode()
                ),
            )

        def instant():
            return datetime.now(UTC).isoformat()

        for device in ("fixture-relay", "fixture-temp"):
            report("a", device, "status", {"status": "online", "last_seen": instant()})
        report(
            "a",
            "fixture-relay",
            "state",
            {
                "state": {"on": False},
                "timestamp": instant(),
                "correlation_id": "startup-relay",
            },
        )
        with pytest.raises(InvalidReferenceError):
            report(
                "b",
                "fixture-temp",
                "state",
                {
                    "state": {"temperature": 99},
                    "timestamp": instant(),
                    "correlation_id": "foreign-temp",
                },
            )
        assert client.get("/devices/fixture-temp", headers=owner).json()["state"] == {}
        report(
            "a",
            "fixture-temp",
            "state",
            {
                "state": {"temperature": 22.5},
                "timestamp": instant(),
                "correlation_id": "startup-temp",
            },
        )
        sensor = client.get("/devices/fixture-temp", headers=owner).json()
        assert sensor["online"] and sensor["state"]["temperature"] == 22.5
        assert sensor["numeric_observed_at"]["temperature"]
        command_headers = {**owner, "Idempotency-Key": str(uuid4())}
        assert (
            client.post(
                "/v1/devices/fixture-temp/commands",
                json={"on": True},
                headers=command_headers,
            ).status_code
            == 422
        )
        response = client.post(
            "/v1/devices/fixture-relay/commands",
            json={"on": True},
            headers=command_headers,
        )
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert client.get("/devices/fixture-relay", headers=owner).json()["state"] == {
            "on": False
        }
        report(
            "a",
            "fixture-relay",
            "ack",
            {
                "command_id": receipt["command_id"],
                "correlation_id": receipt["correlation_id"],
                "status": "applied",
            },
        )
        report(
            "a",
            "fixture-relay",
            "state",
            {
                "state": {"on": True},
                "timestamp": instant(),
                "correlation_id": receipt["correlation_id"],
            },
        )
        applied = client.get(
            "/v1/houses/a/commands/" + receipt["command_id"],
            headers=owner,
        ).json()
        assert applied["status"] == "applied" and applied["reported_on"] is True
        assert client.get("/devices/fixture-relay", headers=owner).json()["state"] == {
            "on": True
        }
