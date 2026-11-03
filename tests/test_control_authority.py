"""Conditional authority against real DB/Gateway; no production admission opt-in."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, UnavailableError
from app.db import Base
from app.events import EventBus
from app.models import ControlCommandORM, DeviceCommandORM, UserORM
from app.repositories.command_repository import CommandRepository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.services.command_service import CommandService
from app.services.control_authority import ControlAuthority, PROFILE
from app.services.control_clock import ControlClock, PERMIT_US
from app.transports.control_models import PermitRequest, decode_request
from app.transports.mqtt import MQTTGateway
from app.transports.mqtt_client import FakeMQTTClient, MQTTMessage
from app.transports.mqtt_models import AckEnvelope, StateEnvelope
from app.transports.mqtt_topics import (
    TopicKind,
    build_topic,
    parse_topic,
    subscription_topics,
)
from test_physical_acceptance import stack

HOUSE, DEVICE = "fixture_house", "fixture_relay"


@pytest.fixture(autouse=True)
def preserve_logger_state():
    # Alembic fileConfig in the reused fixture disables existing loggers.
    # Isolate that test setup effect, as the lifecycle fixture already does.
    previous = {
        value: value.disabled
        for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger)
    }
    yield
    for logger, disabled in previous.items():
        logger.disabled = disabled


class Time:
    def __init__(self):
        self.wall = datetime(2026, 11, 2, tzinfo=UTC)
        self.tick = 0
        self.utc_ok = True

    def advance(self, us):
        self.tick += us
        self.wall += timedelta(microseconds=us)

    def clock(self, qualified=True):
        return ControlClock(
            ticks=lambda: self.tick,
            wall=lambda: self.wall,
            qualified=qualified,
            utc_health=lambda: self.utc_ok,
        )


@asynccontextmanager
async def harness(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as (api, _, _, _, engine):
        time = Time()
        clock = time.clock()
        # Conditional clock/gate are injected solely in this isolated fixture.
        authority = ControlAuthority(clock, gate=lambda *args: None)
        with Session(engine) as session:
            authority.initialize(session)
        time.advance(clock.fence_us)
        wire = FakeMQTTClient()

        async def identity(house, device):
            with Session(engine) as session:
                PhysicalDeviceRepository(session).for_house(house, device)

        async def stage(house, device, identifier):
            with Session(engine) as session:
                return authority.stage(session, house, device, identifier)

        async def request(house, device, value):
            with Session(engine) as session:
                return authority.request(session, house, device, value)

        gateway = MQTTGateway(
            wire,
            EventBus(),
            AsyncMock(),
            AsyncMock(),
            identity,
            control_authority=authority,
            control_stage_handler=stage,
            control_request_handler=request,
        )
        gateway.connected = gateway.control_subscribed = True
        monkeypatch.setattr(
            "app.transports.mqtt.monotonic", lambda: time.tick / 1_000_000
        )

        def seed(lifetime_us=30_000_000, prepare=True):
            now, _ = clock.sample()
            identifier = str(uuid4())
            with Session(engine) as session:
                owner = session.scalar(
                    select(UserORM).where(UserORM.email == "owner@example.test")
                )
                row = DeviceCommandORM(
                    command_id=identifier,
                    correlation_id=str(uuid4()),
                    house_id=HOUSE,
                    device_id=DEVICE,
                    requested_by=owner.id,
                    idempotency_hash=uuid4().hex * 2,
                    requested_on=True,
                    status="queued",
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(microseconds=lifetime_us),
                )
                if prepare:
                    authority.prepare(session, row)
                else:
                    session.add(row)
                    session.commit()
            return identifier

        yield engine, time, authority, wire, gateway, seed, api


def make_request(engine, identifier, **changes):
    with Session(engine) as session:
        record = session.get(ControlCommandORM, identifier)
        data = dict(
            control_version=1,
            type="permit_request",
            execution_profile=PROFILE,
            core_epoch=record.authority_epoch,
            command_id=identifier,
            boot_id="a" * 32,
            session_id="b" * 32,
            nonce="c" * 32,
            intent_sha256=record.wire_sha256,
        )
    return PermitRequest(**{**data, **changes})


def direct(authority, engine, request, house=HOUSE, device=DEVICE):
    with Session(engine) as session:
        return authority.request(session, house, device, request)


def test_exact_candidate_grant_and_durable_replay_binding(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            wire,
            gateway,
            seed,
            _,
        ):
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            topic, payload, qos, retained = wire.published[-1]
            assert topic.endswith("/control/command") and (qos, retained) == (1, False)
            with Session(engine) as session:
                record = session.get(ControlCommandORM, identifier)
                assert record.wire_payload == payload
                assert record.wire_sha256 == sha256(payload.encode()).hexdigest()
                original_e = session.get(DeviceCommandORM, identifier).expires_at
            request = make_request(engine, identifier)
            await gateway.handle_message(
                MQTTMessage(
                    build_topic(HOUSE, DEVICE, TopicKind.CONTROL_REQUEST),
                    request.model_dump_json().encode(),
                )
            )
            first = wire.published[-1]
            grant = json.loads(first[1])
            assert (
                grant["type"] == "permit_grant"
                and 0 < grant["remaining_us"] < PERMIT_US
            )
            assert (
                grant["nonce"] == request.nonce
                and grant["intent_sha256"] == request.intent_sha256
            )
            assert (
                datetime.fromisoformat(grant["expires_at"]).replace(tzinfo=None)
                == original_e
            )
            time.advance(1_000_000)
            await gateway.handle_message(
                MQTTMessage(
                    build_topic(HOUSE, DEVICE, TopicKind.CONTROL_REQUEST),
                    request.model_dump_json().encode(),
                )
            )
            assert wire.published[-1] == first  # Never renew C/R on redelivery.
            with pytest.raises(ConflictError):
                await gateway.send_control_command(HOUSE, DEVICE, identifier)
            authority.replies.clear()  # Lost/evicted RAM cache must not rearm a command.
            with pytest.raises(ConflictError):
                direct(authority, engine, request)
            with Session(engine) as session:
                assert session.get(ControlCommandORM, identifier).permit_binding_sha256
                assert (
                    session.get(DeviceCommandORM, identifier).expires_at == original_e
                )

    asyncio.run(check())


@pytest.mark.parametrize(
    "change",
    ["digest", "nonce", "session", "boot", "epoch", "expired", "terminal", "deadline"],
)
def test_negative_and_replay_requests_never_grant(tmp_path, monkeypatch, change):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            _,
            gateway,
            seed,
            _,
        ):
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            request = make_request(engine, identifier)
            if change in ("nonce", "session", "boot"):
                direct(authority, engine, request)
                field = {"nonce": "nonce", "session": "session_id", "boot": "boot_id"}[
                    change
                ]
                request = request.model_copy(update={field: "d" * 32})
            elif change == "digest":
                request = request.model_copy(update={"intent_sha256": "0" * 64})
            elif change == "epoch":
                request = request.model_copy(update={"core_epoch": str(uuid4())})
            elif change == "expired":
                time.advance(30_000_000)
            else:
                with Session(engine) as session:
                    row = session.get(DeviceCommandORM, identifier)
                    if change == "terminal":
                        row.status = "failed"
                    else:
                        row.expires_at += timedelta(seconds=30)
                    session.commit()
            with pytest.raises(ConflictError):
                direct(authority, engine, request)

    asyncio.run(check())


def test_delayed_commit_and_reply_are_not_refreshed(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            wire,
            gateway,
            seed,
            _,
        ):
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            request = make_request(engine, identifier)
            original = gateway.control_request_handler

            async def delayed(*args):
                reply = await original(*args)
                time.advance(3_000_000)
                return reply

            gateway.control_request_handler = delayed
            await gateway.handle_message(
                MQTTMessage(
                    build_topic(HOUSE, DEVICE, TopicKind.CONTROL_REQUEST),
                    request.model_dump_json().encode(),
                )
            )
            assert json.loads(wire.published[-1][1])["type"] == "permit_denied"
            with pytest.raises(ConflictError):
                direct(authority, engine, request)

    asyncio.run(check())


def test_restart_fence_blocks_old_epoch_even_with_wrong_boot_utc(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            old,
            _,
            gateway,
            seed,
            _,
        ):
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            request = make_request(engine, identifier)
            direct(old, engine, request)
            time.wall += timedelta(
                days=365
            )  # New wall clock must not expire a row early.
            restarted = ControlAuthority(time.clock(), gate=lambda *args: None)
            with Session(engine) as session:
                restarted.initialize(session)
            assert restarted.clock.epoch != old.clock.epoch
            with Session(engine) as session:
                service = CommandService(
                    CommandRepository(session), control_authority=restarted
                )
                with pytest.raises(UnavailableError):
                    service.get(HOUSE, identifier)
                row = session.get(DeviceCommandORM, identifier)
                assert row.status == "queued"
            with pytest.raises(UnavailableError):
                direct(restarted, engine, request)
            time.advance(restarted.clock.fence_us)
            with pytest.raises(ConflictError):
                direct(restarted, engine, request)
            with Session(engine) as session:
                service = CommandService(
                    CommandRepository(session), control_authority=restarted
                )
                assert service.get(HOUSE, identifier).status == "timed_out"
            assert not restarted.replies

    asyncio.run(check())


def test_clock_health_and_closed_gate_are_independent(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            _,
            gateway,
            seed,
            _,
        ):
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            request = make_request(engine, identifier)
            time.utc_ok = False
            with pytest.raises(UnavailableError):
                direct(authority, engine, request)
            time.utc_ok = True
            time.wall += timedelta(seconds=60)
            with pytest.raises(UnavailableError):
                direct(authority, engine, request)
            time.wall -= timedelta(seconds=60)
            with pytest.raises(UnavailableError):
                direct(authority, engine, request)
            with Session(engine) as session:
                assert not authority.expired(session.get(ControlCommandORM, identifier))
            time.advance(30_000_000)
            with Session(engine) as session:
                assert authority.expired(session.get(ControlCommandORM, identifier))
            # Qualified timing never overrides the default physical gate.
            closed = ControlAuthority(time.clock())
            time.advance(closed.clock.fence_us)
            with pytest.raises(ConflictError):
                direct(closed, engine, request)

    asyncio.run(check())


def test_control_parser_topics_and_direction_are_strict():
    for kind in (
        TopicKind.CONTROL_COMMAND,
        TopicKind.CONTROL_REQUEST,
        TopicKind.CONTROL_REPLY,
    ):
        assert parse_topic(build_topic("house", "device", kind)).kind is kind
    assert ("kzhome/v1/+/+/control/request", 1) in subscription_topics()
    for topic in (
        "kzhome/v1/h/d/control/request/x",
        "kzhome/v1/h/d/control/set",
        "kzhome/v1/h/+/control/request",
        "kzhome/v1/h/d/status/x",
    ):
        with pytest.raises(ValueError):
            parse_topic(topic)
    for raw in (
        b"{}",
        b"[]",
        b'{"control_version":1,"control_version":1}',
        b"x" * 4097,
    ):
        with pytest.raises(ValueError):
            decode_request(raw)


def test_unqualified_clock_never_recovers_and_rollback_latches():
    time = Time()
    clock = time.clock(qualified=False)
    time.advance(1_000_000_000)
    with pytest.raises(UnavailableError):
        clock.sample()
    clock = time.clock()
    time.advance(clock.fence_us - 1)
    with pytest.raises(UnavailableError):
        clock.sample()
    time.advance(1)
    clock.sample()
    time.tick -= 1
    with pytest.raises(UnavailableError):
        clock.tick()
    time.advance(1_000_000)
    with pytest.raises(UnavailableError):
        clock.tick()


def config(connection):
    result = Config()
    result.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    result.attributes["connection"] = connection
    return result


def test_migration_preserves_ledger_and_refuses_populated_control_downgrade(
    tmp_path, monkeypatch
):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            _,
            authority,
            _,
            gateway,
            seed,
            _,
        ):
            identifier = seed(prepare=False)
            with engine.begin() as connection:
                before = (
                    connection.execute(text("SELECT * FROM device_commands"))
                    .mappings()
                    .all()
                )
                command.downgrade(config(connection), "0006_command_lifecycle")
                command.upgrade(config(connection), "head")
                assert (
                    connection.execute(text("SELECT * FROM device_commands"))
                    .mappings()
                    .all()
                    == before
                )
                context = MigrationContext.configure(
                    connection,
                    opts={
                        "include_object": lambda obj,
                        name,
                        kind,
                        reflected,
                        compared: kind != "table" or name == "command_control"
                    },
                )
                assert compare_metadata(context, Base.metadata) == []
            with Session(engine) as session:
                session.get(DeviceCommandORM, identifier).status = "failed"
                session.commit()
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            direct(authority, engine, make_request(engine, identifier))
            with engine.begin() as connection:
                with pytest.raises(RuntimeError, match="control history is not empty"):
                    command.downgrade(config(connection), "0006_command_lifecycle")
            with Session(engine) as session:
                assert session.get(ControlCommandORM, identifier).permit_binding_sha256

    asyncio.run(check())


def test_foreign_retained_and_wrong_direction_frames_cannot_grant(
    tmp_path, monkeypatch
):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            _,
            authority,
            wire,
            gateway,
            seed,
            _,
        ):
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            request = make_request(engine, identifier)
            raw = request.model_dump_json().encode()
            count = len(wire.published)
            for kind in (TopicKind.CONTROL_COMMAND, TopicKind.CONTROL_REPLY):
                with pytest.raises(ValueError):
                    await gateway.handle_message(
                        MQTTMessage(build_topic(HOUSE, DEVICE, kind), raw)
                    )
            with pytest.raises(ValueError):
                await gateway.handle_message(
                    MQTTMessage(
                        build_topic(HOUSE, DEVICE, TopicKind.CONTROL_REQUEST), raw, True
                    )
                )
            # Topic identity is authoritative, even for otherwise valid bytes.
            from app.core.errors import EntityNotFoundError

            with pytest.raises(EntityNotFoundError):
                await gateway.handle_message(
                    MQTTMessage(
                        build_topic("foreign", DEVICE, TopicKind.CONTROL_REQUEST), raw
                    )
                )
            with pytest.raises(EntityNotFoundError):
                direct(authority, engine, request, device="another-device")
            assert len(wire.published) == count
            with Session(engine) as session:
                assert (
                    session.get(ControlCommandORM, identifier).permit_binding_sha256
                    is None
                )

    asyncio.run(check())


@pytest.mark.parametrize(
    "field,value",
    [
        ("control_version", True),
        ("control_version", 1.0),
        ("control_version", 2),
        ("nonce", "A" * 32),
        ("nonce", "1" * 31),
        ("intent_sha256", "x" * 64),
        ("command_id", "non-uuid"),
        ("mqtt_password", "synthetic-unexpected-field"),
    ],
)
def test_strict_request_field_validation(field, value):
    body = dict(
        control_version=1,
        type="permit_request",
        execution_profile=PROFILE,
        core_epoch=str(uuid4()),
        command_id=str(uuid4()),
        boot_id="a" * 32,
        session_id="b" * 32,
        nonce="c" * 32,
        intent_sha256="d" * 64,
    )
    body[field] = value
    with pytest.raises(ValueError):
        decode_request(json.dumps(body).encode())


def test_current_epoch_lifecycle_uses_original_counter_not_jumped_wall(
    tmp_path, monkeypatch
):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            _,
            gateway,
            seed,
            _,
        ):
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            time.wall += timedelta(seconds=3600)
            with pytest.raises(UnavailableError):
                authority.clock.sample()
            with Session(engine) as session:
                service = CommandService(
                    CommandRepository(session), control_authority=authority
                )
                assert service.get(HOUSE, identifier).status == "queued"
            time.advance(30_000_000)
            with Session(engine) as session:
                # Even the software clock being unusable cannot prevent safe control expiry.
                service = CommandService(
                    CommandRepository(session),
                    control_authority=authority,
                    now=lambda: (_ for _ in ()).throw(UnavailableError("wall fault")),
                )
                assert service.get(HOUSE, identifier).status == "timed_out"
            assert not authority.replies

    asyncio.run(check())


def test_production_wiring_never_issues_grants_or_control_candidates(
    tmp_path, monkeypatch
):
    async def check():
        async with stack(tmp_path, monkeypatch) as (api, wire, _, _, _):
            gateway = api.client._transport.app.state.mqtt_gateway
            body = dict(
                control_version=1,
                type="permit_request",
                execution_profile=PROFILE,
                core_epoch=str(uuid4()),
                command_id=str(uuid4()),
                boot_id="a" * 32,
                session_id="b" * 32,
                nonce="c" * 32,
                intent_sha256="d" * 64,
            )
            await gateway.handle_message(
                MQTTMessage(
                    build_topic(HOUSE, DEVICE, TopicKind.CONTROL_REQUEST),
                    json.dumps(body).encode(),
                )
            )
            assert len(wire.published) == 1
            assert json.loads(wire.published[0][1])["type"] == "permit_denied"
            with pytest.raises(ConflictError):
                await gateway.send_control_command(HOUSE, DEVICE, body["command_id"])
            assert len(wire.published) == 1

    asyncio.run(check())


def test_repository_acl_adds_only_server_control_directions():
    content = (Path(__file__).resolve().parents[1] / "deploy/mosquitto/acl").read_text()
    sections = {}
    user = None
    for line in content.splitlines():
        if line.startswith("user "):
            user = line[5:]
            sections[user] = set()
        elif line.startswith("topic "):
            sections[user].add(line)
    core = sections["kzhome-core"]
    assert {line for line in core if "/control/" in line} == {
        "topic read kzhome/v1/+/+/control/request",
        "topic write kzhome/v1/+/+/control/command",
        "topic write kzhome/v1/+/+/control/reply",
    }
    for user in ("relay-simulator", "motion-simulator"):
        assert not any("/control/" in line for line in sections[user])
    assert "topic read kzhome/v1/e2e_house/e2e_relay/set" in sections["relay-simulator"]


def test_limiter_and_cache_loss_do_not_rearm_permit(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            wire,
            gateway,
            seed,
            _,
        ):
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            request = make_request(engine, identifier)
            raw = request.model_dump_json().encode()
            topic = build_topic(HOUSE, DEVICE, TopicKind.CONTROL_REQUEST)
            for _ in range(5):
                await gateway.handle_message(MQTTMessage(topic, raw))
            assert (
                len(wire.published) == 2
            )  # One candidate and one grant, then limiter.
            authority.replies.clear()
            time.advance(1_000_000)
            await gateway.handle_message(MQTTMessage(topic, raw))
            assert json.loads(wire.published[-1][1])["type"] == "permit_denied"

    asyncio.run(check())


def test_nonce_cannot_be_reused_for_a_different_command(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            _,
            gateway,
            seed,
            _,
        ):
            first = seed()
            await gateway.send_control_command(HOUSE, DEVICE, first)
            direct(authority, engine, make_request(engine, first))
            with Session(engine) as session:
                session.get(DeviceCommandORM, first).status = "failed"
                session.commit()
            time.advance(1_000_000)
            second = seed()
            await gateway.send_control_command(HOUSE, DEVICE, second)
            with pytest.raises(ConflictError, match="nonce was already used"):
                direct(authority, engine, make_request(engine, second))
            with Session(engine) as session:
                assert session.get(ControlCommandORM, first).permit_binding_sha256
                assert (
                    session.get(ControlCommandORM, second).permit_binding_sha256 is None
                )

    asyncio.run(check())


def test_denied_control_subscription_preserves_legacy_gateway():
    async def check():
        class Broker(FakeMQTTClient):
            async def subscribe(self, topic, qos):
                if "/control/" in topic:
                    raise PermissionError("control ACL not commissioned")
                await super().subscribe(topic, qos)

            async def messages(self):
                if False:
                    yield None

        wire = Broker()
        bus = EventBus()
        connected = []
        gateway = MQTTGateway(wire, bus, AsyncMock(), AsyncMock(), AsyncMock())

        async def observe(event):
            if event.type == "mqtt_connected":
                connected.append(True)
                assert not gateway.control_subscribed
                gateway._stopping = True

        bus.subscribe(observe)
        await gateway.run()
        assert connected == [True]
        assert wire.subscriptions == [
            pair for pair in subscription_topics() if "/control/" not in pair[0]
        ]

    asyncio.run(check())


def test_timing_cache_only_evicts_terminal_evidence(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            _,
            gateway,
            seed,
            _,
        ):
            authority.MAX_WINDOWS = 1
            first = seed()
            await gateway.send_control_command(HOUSE, DEVICE, first)
            time.advance(30_000_000)
            assert first in authority.windows  # Pending expiry is not evicted blindly.
            with Session(engine) as session:
                service = CommandService(
                    CommandRepository(session), control_authority=authority
                )
                assert service.get(HOUSE, first).status == "timed_out"
            second = seed()
            await gateway.send_control_command(HOUSE, DEVICE, second)
            assert first not in authority.windows and second in authority.windows
            with pytest.raises(ConflictError):
                direct(authority, engine, make_request(engine, first))

    asyncio.run(check())


def test_historical_ledger_is_never_adopted_by_control_authority(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            _,
            authority,
            wire,
            gateway,
            seed,
            _,
        ):
            identifier = seed(prepare=False)
            with pytest.raises(ConflictError):
                await gateway.send_control_command(HOUSE, DEVICE, identifier)
            with Session(engine) as session:
                old = session.get(DeviceCommandORM, identifier)
                with pytest.raises(ConflictError):
                    authority.prepare(session, old)
                assert session.get(ControlCommandORM, identifier) is None
                assert old.status == "queued" and old.dispatch_started_at is None
            assert wire.published == []

    asyncio.run(check())


def test_control_evidence_needs_a_permit_and_cannot_complete_after_deadline(
    tmp_path, monkeypatch
):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            authority,
            _,
            gateway,
            seed,
            _,
        ):
            first = seed()
            await gateway.send_control_command(HOUSE, DEVICE, first)

            def evidence(identifier):
                with Session(engine) as session:
                    row = session.get(DeviceCommandORM, identifier)
                    return (
                        AckEnvelope(
                            command_id=identifier,
                            correlation_id=row.correlation_id,
                            status="applied",
                        ),
                        StateEnvelope(
                            timestamp=time.wall,
                            correlation_id=row.correlation_id,
                            state={"on": True},
                        ),
                    )

            def report(identifier):
                ack, state = evidence(identifier)
                with Session(engine) as session:
                    service = CommandService(
                        CommandRepository(session), control_authority=authority
                    )
                    service.ack(HOUSE, DEVICE, ack)
                    service.state(HOUSE, DEVICE, state)
                    return service.get(HOUSE, identifier)

            result = report(first)
            assert (
                result.status == "queued"
                and result.ack_status is None
                and result.reported_on is None
            )
            direct(authority, engine, make_request(engine, first))
            result = report(first)
            assert (
                result.status == "applied" and result.completed_at < result.expires_at
            )
            assert (
                not {"wire_payload", "wire_sha256", "permit_binding_sha256"}
                & result.model_dump().keys()
            )
            time.advance(1_000_000)
            second = seed()
            await gateway.send_control_command(HOUSE, DEVICE, second)
            direct(authority, engine, make_request(engine, second, nonce="e" * 32))
            time.advance(30_000_000)
            assert report(second).status == "timed_out"

    asyncio.run(check())


def test_failed_control_insert_rolls_back_ledger_as_well(tmp_path, monkeypatch):
    async def check():
        async with harness(tmp_path, monkeypatch) as (engine, _, _, wire, _, seed, _):
            monkeypatch.setattr(
                "app.services.control_authority.digest", lambda _: "invalid"
            )
            with pytest.raises(ConflictError):
                seed()
            with Session(engine) as session:
                assert session.query(DeviceCommandORM).count() == 0
                assert session.query(ControlCommandORM).count() == 0
            assert wire.published == []

    asyncio.run(check())


def test_foreign_epoch_created_after_recovery_snapshot_cannot_expire_early(
    tmp_path, monkeypatch
):
    async def check():
        async with harness(tmp_path, monkeypatch) as (
            engine,
            time,
            owner,
            _,
            gateway,
            seed,
            _,
        ):
            observer = ControlAuthority(time.clock(), gate=lambda *args: None)
            with Session(engine) as session:
                observer.initialize(session)  # No old pending command in this cohort.
            time.advance(observer.clock.fence_us)
            identifier = seed()
            await gateway.send_control_command(HOUSE, DEVICE, identifier)
            with Session(engine) as session:
                service = CommandService(
                    CommandRepository(session), control_authority=observer
                )
                with pytest.raises(UnavailableError, match="no recovery evidence"):
                    service.get(HOUSE, identifier)
                assert session.get(DeviceCommandORM, identifier).status == "queued"
            with pytest.raises(ConflictError):
                direct(observer, engine, make_request(engine, identifier))
            assert direct(owner, engine, make_request(engine, identifier)).grant

    asyncio.run(check())
