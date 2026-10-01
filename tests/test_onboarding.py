"""Physical lifecycle security checks, without real secrets, broker or hardware."""

import asyncio
import json
from secrets import token_urlsafe
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import Settings
from app.core.errors import ConflictError
from app.db import Base
from app.events import EventBus
from app.models import (
    DeviceORM,
    EventLogORM,
    FloorORM,
    HouseMembershipORM,
    HouseORM,
    PhysicalDeviceORM,
    RoomORM,
    UserORM,
)
from app.repositories import DeviceRepository, RoomRepository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas import DeviceCreate
from app.schemas.onboarding import DeviceClaim, InventoryRegistration
from app.security import TokenCodec
from app.services.device_service import DeviceService
from app.services.onboarding_service import OnboardingService
from app.transports import FakeMQTTClient
from app.transports.mqtt_client import MQTTMessage


@pytest.fixture
def onboarding(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    from app.main import create_app

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'onboarding.db'}",
        app_env="test",
        mqtt_enabled=True,
        mqtt_host="offline",
        mqtt_client_id="offline-core",
    )
    engine = create_engine(settings.database_url)

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    codec = TokenCodec(settings.auth_jwt_secret, "HS256", 15, 30)
    headers = {}
    with Session(engine) as session:
        session.add_all([HouseORM(id="a", name="A"), HouseORM(id="b", name="B")])
        session.flush()
        session.add_all(
            [
                FloorORM(id="fa", house_id="a", name="A", order=0),
                FloorORM(id="fa2", house_id="a", name="A2", order=1),
                FloorORM(id="fb", house_id="b", name="B", order=0),
            ]
        )
        session.flush()
        session.add_all(
            [
                RoomORM(id="ra", floor_id="fa", name="A"),
                RoomORM(id="ra2", floor_id="fa2", name="A2"),
                RoomORM(id="rb", floor_id="fb", name="B"),
            ]
        )
        for role in (
            "owner",
            "installer",
            "technician",
            "resident",
            "foreign",
            "outsider",
            "both",
        ):
            # JWT tests do not need login/password work. No actual credentials.
            session.add(
                UserORM(id=role, email=f"{role}@example.test", password_hash="unused")
            )
            session.flush()
            if role != "outsider":
                houses = (
                    ["a", "b"]
                    if role == "both"
                    else ["b" if role == "foreign" else "a"]
                )
                for house in houses:
                    session.add(
                        HouseMembershipORM(
                            id=f"{role}-{house}",
                            user_id=role,
                            house_id=house,
                            role="owner" if role in ("foreign", "both") else role,
                        )
                    )
            token, _ = codec.issue(role, "access")
            headers[role] = {"Authorization": f"Bearer {token}"}
        session.commit()
    code = token_urlsafe(32)
    registration = InventoryRegistration(
        device_id="physical1",
        hardware_id="serial-c6-001",
        hardware_model="esp32-c6-relay-v1",
        claim_code=code,
    )
    with Session(engine) as session:
        OnboardingService(PhysicalDeviceRepository(session)).register_inventory(
            registration
        )
    wire = FakeMQTTClient()
    app = create_app(settings, run_simulator=False, mqtt_client=wire)
    try:
        with TestClient(app) as client:
            yield client, engine, headers, code, wire, app
    finally:
        engine.dispose()


def claim_body(code, **patch):
    return {
        "hardware_id": "serial-c6-001",
        "claim_code": code,
        "room_id": "ra",
        "name": "Hall relay",
        **patch,
    }


BASE = "/houses/a/physical-devices/physical1"


def claim_device(stack, role="installer"):
    client, _, headers, code, *_ = stack
    result = client.post(
        "/houses/a/device-claims", json=claim_body(code), headers=headers[role]
    )
    assert result.status_code == 201, result.text
    return result.json()


def activate(stack, role="installer"):
    client, _, headers, *_ = stack
    return client.post(
        BASE + "/activate",
        json={"broker_access_confirmed": True},
        headers=headers[role],
    )


@pytest.mark.parametrize(
    "role,status",
    [
        (None, 401),
        ("outsider", 404),
        ("foreign", 404),
        ("resident", 403),
        ("technician", 403),
    ],
)
def test_claim_requires_existing_house_onboarding_permission(onboarding, role, status):
    client, engine, headers, code, *_ = onboarding
    result = client.post(
        "/houses/a/device-claims", json=claim_body(code), headers=headers.get(role, {})
    )
    assert result.status_code == status
    with Session(engine) as session:
        assert session.get(PhysicalDeviceORM, "physical1").status == "unprovisioned"
        assert session.get(DeviceORM, "physical1") is None


@pytest.mark.parametrize("role", ["owner", "installer"])
def test_claim_creates_offline_profile_consumes_code_and_audits(onboarding, role):
    client, engine, headers, code, *_ = onboarding
    result = claim_device(onboarding, role)
    assert result["status"] == "provisioning"
    assert result["house_id"] == "a" and result["device_id"] == "physical1"
    assert result["protocol_version"] == "v1"
    device = client.get("/devices/physical1", headers=headers["resident"]).json()
    assert device["type"] == "relay" and device["capabilities"] == ["on_off"]
    assert (
        device["state"] == {} and device["online"] is False and device["metadata"] == {}
    )
    with Session(engine) as session:
        item = session.get(PhysicalDeviceORM, "physical1")
        assert item.claim_code_hash is None and item.bound_device_id == "physical1"
        log = session.scalar(
            select(EventLogORM).where(
                EventLogORM.event_type == "device_onboarding_changed"
            )
        )
        assert log.payload == {
            "device_id": "physical1",
            "actor_id": role,
            "status": "provisioning",
        }
        assert code not in json.dumps(log.payload)


@pytest.mark.parametrize(
    "patch",
    [
        {"room_id": "rb"},
        {"room_id": "missing"},
        {"hardware_id": "missing"},
        {"claim_code": "X" * 43},
    ],
)
def test_wrong_identity_proof_or_room_is_hidden_and_rolls_back(onboarding, patch):
    client, engine, headers, code, *_ = onboarding
    result = client.post(
        "/houses/a/device-claims",
        json=claim_body(code, **patch),
        headers=headers["owner"],
    )
    assert result.status_code == 404
    with Session(engine) as session:
        assert session.get(PhysicalDeviceORM, "physical1").claim_code_hash is not None
        assert session.get(DeviceORM, "physical1") is None
        assert session.scalar(select(func.count()).select_from(EventLogORM)) == 0
    claim_device(onboarding)


def test_claim_is_not_silently_idempotent_or_reclaimable(onboarding):
    client, engine, headers, code, *_ = onboarding
    original = claim_device(onboarding)
    assert (
        client.post(
            "/houses/a/device-claims", json=claim_body(code), headers=headers["owner"]
        ).status_code
        == 409
    )
    for role in ("foreign", "both"):
        result = client.post(
            "/houses/b/device-claims",
            json=claim_body(code, room_id="rb"),
            headers=headers[role],
        )
        assert result.status_code == 404
    assert client.get(BASE, headers=headers["installer"]).json() == original
    with Session(engine) as session:
        assert session.get(DeviceORM, "physical1").room_id == "ra"
        assert session.scalar(select(func.count()).select_from(PhysicalDeviceORM)) == 1


def test_inventory_is_private_and_ordinary_responses_contain_no_secrets(onboarding):
    client, engine, headers, code, *_ = onboarding
    with Session(engine) as session:
        digest = session.get(PhysicalDeviceORM, "physical1").claim_code_hash
        assert len(digest) == 64 and code not in digest
    assert (
        client.get("/houses/a/physical-devices", headers=headers["owner"]).json() == []
    )
    assert client.get(BASE, headers=headers["owner"]).status_code == 404
    claim_device(onboarding)
    for path in (
        "/devices",
        "/devices/physical1",
        BASE,
        "/houses/a/physical-devices",
        "/events",
    ):
        result = client.get(path, headers=headers["owner"])
        assert result.status_code == 200
        for secret in (code, digest, "claim_code", "mqtt_password", "password_hash"):
            assert secret not in result.text


@pytest.mark.parametrize(
    "patch",
    [
        {"claim_code": "secret-short"},
        {"claim_code": "/" * 43},
        {"mqtt_password": "do-not-echo"},
        {"hardware_model": "do-not-echo"},
    ],
)
def test_validation_errors_never_echo_input(onboarding, patch, caplog):
    client, _, headers, code, *_ = onboarding
    result = client.post(
        "/houses/a/device-claims",
        json=claim_body(code, **patch),
        headers=headers["owner"],
    )
    assert result.status_code == 422
    assert code not in result.text and "do-not-echo" not in result.text
    assert "secret-short" not in result.text
    assert code not in caplog.text and "do-not-echo" not in caplog.text


@pytest.mark.parametrize("action", ["activate", "deactivate", "revoke"])
@pytest.mark.parametrize(
    "role,status",
    [(None, 401), ("foreign", 404), ("resident", 403), ("technician", 403)],
)
def test_lifecycle_authorization(onboarding, action, role, status):
    client, _, headers, *_ = onboarding
    claim_device(onboarding)
    body = {"broker_access_confirmed": True} if action == "activate" else None
    assert (
        client.post(
            BASE + "/" + action, json=body, headers=headers.get(role, {})
        ).status_code
        == status
    )


def test_house_scoped_lifecycle_idor_even_for_member_of_both_houses(onboarding):
    client, _, headers, *_ = onboarding
    claim_device(onboarding)
    for role in ("foreign", "both"):
        foreign_base = BASE.replace("/a/", "/b/")
        assert client.get(foreign_base, headers=headers[role]).status_code == 404
        assert (
            client.get("/houses/b/physical-devices", headers=headers[role]).json() == []
        )
        for action in ("activate", "deactivate", "revoke"):
            assert (
                client.post(
                    foreign_base + "/" + action,
                    json={"broker_access_confirmed": True},
                    headers=headers[role],
                ).status_code
                == 404
            )


def test_activation_requires_explicit_broker_confirmation_and_is_idempotent(onboarding):
    client, engine, headers, *_ = onboarding
    claim_device(onboarding)
    for body in (
        {},
        {"broker_access_confirmed": False},
        {"broker_access_confirmed": True, "password": "hidden"},
    ):
        assert (
            client.post(
                BASE + "/activate", json=body, headers=headers["owner"]
            ).status_code
            == 422
        )
    first = activate(onboarding)
    assert first.status_code == 200 and first.json()["status"] == "active"
    assert activate(onboarding).json() == first.json()
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(EventLogORM)) == 2


def test_deactivation_reactivation_and_terminal_revocation(onboarding):
    client, engine, headers, code, *_ = onboarding
    claim_device(onboarding)
    assert activate(onboarding).status_code == 200
    for action, expected in (("deactivate", "inactive"), ("revoke", "revoked")):
        first = client.post(BASE + "/" + action, headers=headers["installer"])
        assert first.json()["status"] == expected
        assert (
            client.post(BASE + "/" + action, headers=headers["installer"]).json()
            == first.json()
        )
        if action == "deactivate":
            assert activate(onboarding).status_code == 200
    assert activate(onboarding).status_code == 409
    assert (
        client.post(BASE + "/deactivate", headers=headers["owner"]).status_code == 409
    )
    assert (
        client.post(
            "/houses/a/device-claims", json=claim_body(code), headers=headers["owner"]
        ).status_code
        == 409
    )
    with Session(engine) as session:
        assert session.get(PhysicalDeviceORM, "physical1").house_id == "a"
        assert session.get(DeviceORM, "physical1").online is False


@pytest.mark.parametrize(
    "phase", ["unprovisioned", "provisioning", "inactive", "revoked"]
)
@pytest.mark.parametrize("kind", ["state", "status", "ack", "telemetry"])
def test_nonactive_physical_mqtt_messages_are_rejected(onboarding, phase, kind):
    client, engine, headers, _, _, app = onboarding
    if phase != "unprovisioned":
        claim_device(onboarding)
        if phase in ("inactive", "revoked"):
            action = "deactivate" if phase == "inactive" else "revoke"
            assert (
                client.post(BASE + "/" + action, headers=headers["owner"]).status_code
                == 200
            )
    payload = {
        "protocol_version": "v1",
        "timestamp": "2026-10-01T00:00:00Z",
        "correlation_id": "test-correlation",
        "state": {"on": True},
        "status": "online" if kind == "status" else "applied",
        "last_seen": "2026-10-01T00:00:00Z",
        "command_id": "test-command",
        "metrics": {"rssi": -55},
    }
    before = client.get("/events", headers=headers["owner"]).json()
    with pytest.raises(ConflictError, match="not active"):
        client.portal.call(
            app.state.mqtt_gateway.handle_message,
            MQTTMessage(f"kzhome/v1/a/physical1/{kind}", json.dumps(payload).encode()),
        )
    assert client.get("/events", headers=headers["owner"]).json() == before
    with Session(engine) as session:
        item = session.get(DeviceORM, "physical1")
        if item:
            assert item.state == {} and item.online is False


def test_active_physical_command_ack_state_status_and_deactivation(onboarding):
    client, engine, headers, _, wire, app = onboarding
    claim_device(onboarding)
    assert (
        client.post("/devices/physical1/on", headers=headers["resident"]).status_code
        == 409
    )
    assert wire.published == []
    assert activate(onboarding).status_code == 200
    command = client.post("/devices/physical1/on", headers=headers["resident"])
    assert command.status_code == 200 and command.json()["state"] == {}
    topic, raw, qos, retained = wire.published[-1]
    assert topic == "kzhome/v1/a/physical1/set" and (qos, retained) == (1, False)
    sent = json.loads(raw)
    for kind, body in (
        (
            "ack",
            {
                "command_id": sent["command_id"],
                "correlation_id": sent["correlation_id"],
                "status": "applied",
            },
        ),
        (
            "state",
            {
                "timestamp": sent["timestamp"],
                "correlation_id": sent["correlation_id"],
                "state": {"on": True},
            },
        ),
        ("status", {"last_seen": sent["timestamp"], "status": "online"}),
        ("telemetry", {"timestamp": sent["timestamp"], "metrics": {"rssi": -55}}),
    ):
        client.portal.call(
            app.state.mqtt_gateway.handle_message,
            MQTTMessage(f"kzhome/v1/a/physical1/{kind}", json.dumps(body).encode()),
        )
    observed = client.get("/devices/physical1", headers=headers["resident"]).json()
    assert observed["state"] == {"on": True} and observed["online"] is True
    assert (
        client.post(BASE + "/deactivate", headers=headers["owner"]).status_code == 200
    )
    assert (
        client.post("/devices/physical1/off", headers=headers["resident"]).status_code
        == 409
    )
    assert len(wire.published) == 1
    with Session(engine) as session:
        assert session.get(DeviceORM, "physical1").state == {"on": True}
        assert session.get(DeviceORM, "physical1").online is False


@pytest.mark.parametrize(
    "values",
    [
        {"state": {"on": True}},
        {"online": True},
        {"capabilities": ["brightness"]},
        {"type": "light"},
        {"metadata": {"mqtt_password": "never-store"}},
    ],
)
def test_physical_profile_observed_state_and_metadata_cannot_be_overwritten(
    onboarding, values
):
    client, _, headers, *_ = onboarding
    claim_device(onboarding)
    assert (
        client.patch(
            "/devices/physical1", json=values, headers=headers["installer"]
        ).status_code
        == 409
    )
    observed = client.get("/devices/physical1", headers=headers["owner"]).json()
    assert (
        observed["metadata"] == {}
        and observed["state"] == {}
        and observed["online"] is False
    )


@pytest.mark.parametrize(
    "kind,body",
    [
        ("devices/physical1", {"room_id": "rb"}),
        ("rooms/ra", {"floor_id": "fb"}),
        ("floors/fa", {"house_id": "b"}),
    ],
)
def test_existing_crud_cannot_transfer_physical_binding(onboarding, kind, body):
    client, _, headers, *_ = onboarding
    claim_device(onboarding)
    assert (
        client.patch("/" + kind, json=body, headers=headers["both"]).status_code == 409
    )
    assert client.get(BASE, headers=headers["owner"]).json()["house_id"] == "a"


@pytest.mark.parametrize(
    "path", ["/devices/physical1", "/rooms/ra", "/floors/fa", "/houses/a"]
)
def test_existing_crud_cannot_erase_physical_identity(onboarding, path):
    client, _, headers, *_ = onboarding
    claim_device(onboarding)
    assert client.post(BASE + "/revoke", headers=headers["owner"]).status_code == 200
    assert client.delete(path, headers=headers["owner"]).status_code == 409
    assert client.get(BASE, headers=headers["owner"]).json()["status"] == "revoked"


def test_same_house_location_and_name_changes_are_allowed(onboarding):
    client, _, headers, *_ = onboarding
    claim_device(onboarding)
    assert (
        client.patch(
            "/rooms/ra", json={"floor_id": "fa2"}, headers=headers["owner"]
        ).status_code
        == 200
    )
    result = client.patch(
        "/devices/physical1",
        json={"room_id": "ra2", "name": "Kitchen"},
        headers=headers["installer"],
    )
    assert result.status_code == 200 and result.json()["name"] == "Kitchen"
    assert client.get(BASE, headers=headers["owner"]).json()["house_id"] == "a"


def test_ordinary_creation_cannot_occupy_reserved_identity(onboarding):
    client, _, headers, *_ = onboarding
    result = client.post(
        "/devices",
        json={"id": "physical1", "room_id": "ra", "name": "Hijack", "type": "relay"},
        headers=headers["installer"],
    )
    assert result.status_code == 409
    claim_device(onboarding)


@pytest.mark.parametrize("stage", ["claim", "audit"])
def test_late_claim_failure_rolls_back_device_binding_code_and_audit(
    onboarding, monkeypatch, stage
):
    _, engine, _, code, *_ = onboarding

    def fail(*args):
        raise RuntimeError("synthetic failure")

    with Session(engine) as session:
        repo = PhysicalDeviceRepository(session)
        monkeypatch.setattr(repo, stage, fail)
        with pytest.raises(RuntimeError):
            OnboardingService(repo).claim("a", DeviceClaim(**claim_body(code)), "owner")
        assert session.get(DeviceORM, "physical1") is None
        item = session.get(PhysicalDeviceORM, "physical1")
        assert (
            item.status == "unprovisioned"
            and item.house_id is None
            and item.claim_code_hash
        )
        assert session.scalar(select(func.count()).select_from(EventLogORM)) == 0
    claim_device(onboarding)


def test_transition_failure_rolls_back_lifecycle_and_online(onboarding, monkeypatch):
    _, engine, _, _, _, _ = onboarding
    claim_device(onboarding)
    activate(onboarding)
    with Session(engine) as session:
        session.get(DeviceORM, "physical1").online = True
        session.commit()
        repo = PhysicalDeviceRepository(session)
        monkeypatch.setattr(
            repo, "audit", lambda *args: (_ for _ in ()).throw(RuntimeError("failure"))
        )
        with pytest.raises(RuntimeError):
            OnboardingService(repo).transition("a", "physical1", "revoked", "owner")
        assert session.get(PhysicalDeviceORM, "physical1").status == "active"
        assert session.get(DeviceORM, "physical1").online is True


def test_inventory_duplicate_and_existing_device_do_not_overwrite(onboarding):
    _, engine, _, code, *_ = onboarding
    with Session(engine) as session:
        service = OnboardingService(PhysicalDeviceRepository(session))
        for device_id, hardware_id in (
            ("physical1", "serial-other"),
            ("other", "serial-c6-001"),
        ):
            with pytest.raises(ConflictError):
                service.register_inventory(
                    InventoryRegistration(
                        device_id=device_id,
                        hardware_id=hardware_id,
                        hardware_model="esp32-c6-relay-v1",
                        claim_code=code,
                    )
                )
        devices = DeviceService(
            DeviceRepository(session), RoomRepository(session), EventBus()
        )
        devices.create(
            DeviceCreate(id="legacy", name="Legacy", room_id="ra", type="relay")
        )
        with pytest.raises(ConflictError):
            service.register_inventory(
                InventoryRegistration(
                    device_id="legacy",
                    hardware_id="serial-legacy",
                    hardware_model="esp32-c6-relay-v1",
                    claim_code=code,
                )
            )
        assert session.scalar(select(func.count()).select_from(PhysicalDeviceORM)) == 1


def test_physical_requires_transport_and_cannot_bypass_binding(onboarding):
    _, engine, *_ = onboarding
    claim_device(onboarding)
    activate(onboarding)
    with Session(engine) as session:
        service = DeviceService(
            DeviceRepository(session), RoomRepository(session), EventBus()
        )
        with pytest.raises(ConflictError, match="require a transport"):
            asyncio.run(service.update_state("physical1", {"on": True}))
        session.rollback()
        # Simulate out-of-band corruption. Even an active identity cannot use it.
        session.get(DeviceORM, "physical1").room_id = "rb"
        session.commit()
        service.transport = AsyncMock()
        with pytest.raises(ConflictError, match="inconsistent"):
            asyncio.run(service.update_state("physical1", {"on": True}))
        service.transport.send_command.assert_not_awaited()

    client, _, headers, *_ = onboarding
    assert (
        client.get("/devices/physical1", headers=headers["foreign"]).status_code == 404
    )
    assert client.get("/devices/physical1", headers=headers["owner"]).status_code == 200
    assert client.get("/devices", headers=headers["foreign"]).json() == []
    assert [
        item["id"] for item in client.get("/devices", headers=headers["owner"]).json()
    ] == ["physical1"]


def test_database_binding_check_rejects_null_bound_device(onboarding):
    _, engine, *_ = onboarding
    with Session(engine) as session:
        item = session.get(PhysicalDeviceORM, "physical1")
        item.status = "active"
        item.house_id = "a"
        item.claim_code_hash = None
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_inactive_user_and_superuser_metadata_do_not_bypass_onboarding(onboarding):
    client, engine, headers, code, *_ = onboarding
    with Session(engine) as session:
        session.get(UserORM, "owner").is_active = False
        session.get(UserORM, "outsider").is_superuser = True
        session.commit()
    assert (
        client.post(
            "/houses/a/device-claims", json=claim_body(code), headers=headers["owner"]
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/houses/a/device-claims",
            json=claim_body(code),
            headers=headers["outsider"],
        ).status_code
        == 404
    )


def test_integrity_conflict_does_not_adopt_or_overwrite_an_existing_device(onboarding):
    _, engine, _, code, *_ = onboarding
    with Session(engine) as session:
        session.add(
            DeviceORM(
                id="physical1",
                name="Existing",
                room_id="rb",
                type="relay",
                capabilities=["on_off"],
                state={"on": True},
                metadata_={},
            )
        )
        session.commit()
        with pytest.raises(ConflictError):
            OnboardingService(PhysicalDeviceRepository(session)).claim(
                "a", DeviceClaim(**claim_body(code)), "owner"
            )
        item = session.get(PhysicalDeviceORM, "physical1")
        assert item.status == "unprovisioned" and item.claim_code_hash is not None
        existing = session.get(DeviceORM, "physical1")
        assert existing.room_id == "rb" and existing.state == {"on": True}


def test_foreign_keys_protect_claimed_device_and_house_even_outside_services(
    onboarding,
):
    _, engine, *_ = onboarding
    claim_device(onboarding)
    with Session(engine) as session:
        for model, identifier in ((DeviceORM, "physical1"), (HouseORM, "a")):
            with pytest.raises(IntegrityError):
                session.execute(
                    model.__table__.delete().where(model.__table__.c.id == identifier)
                )
                session.commit()
            session.rollback()
        assert session.get(PhysicalDeviceORM, "physical1").house_id == "a"


def test_cli_validation_failure_never_prints_claim_code(monkeypatch, capsys):
    from app import register_physical_device as cli

    monkeypatch.setattr(
        "sys.argv",
        [
            "register_physical_device",
            "--device-id",
            "device",
            "--hardware-id",
            "serial",
            "--hardware-model",
            "esp32-c6-relay-v1",
        ],
    )
    monkeypatch.setattr("getpass.getpass", lambda *args: "synthetic-secret")
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    assert "synthetic-secret" not in capsys.readouterr().out
