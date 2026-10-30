"""Upgrade transaction boundaries with owned files and recorded Docker calls."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.demo_relay import handle
from app.transports.mqtt_models import CommandEnvelope, utc_now
from deploy import demo_upgrade as module
from simulator.mqtt_relay import Relay


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    state = {
        "version": 14,
        "project": "kzhome-demo-unit",
        "web_revision": "old",
        "env": {
            "DEMO_CORE_IMAGE": "old-core",
            "DEMO_WEB_IMAGE": "old-web",
            "DEMO_API_PORT": "18080",
            "DEMO_WEB_PORT": "18081",
        },
    }
    for name in (*module.PRIVATE, "compose.env"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("PRIVATE-unchanged")
    (tmp_path / "state.json").write_text(json.dumps(state))
    calls = []

    class Demo:
        path = tmp_path
        project = state["project"]

        def load(self):
            self.state = json.loads((self.path / "state.json").read_text())
            self.env = dict(self.state["env"])

        def dc(self, *args):
            calls.append(args)

        def verify(self):
            calls.append(("verify",))

    demo = Demo()
    demo.load()
    monkeypatch.setattr(
        module, "check", lambda d, w: {"core_tree": "reviewed", "web_revision": "new"}
    )
    monkeypatch.setattr(module, "stage_web", lambda w, c: c.mkdir())
    monkeypatch.setattr(module, "image_ids", lambda env: {"fixture": "image-id"})

    def backup(demo, restart=False):
        directory = tmp_path / "backups" / "owned"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "database.dump").write_bytes(b"PGDMP-test-only")
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "project": demo.project,
                    "files": {
                        "database.dump": module.digest(directory / "database.dump")
                    },
                }
            )
        )
        calls.append(("backup",))
        return directory

    monkeypatch.setattr(module, "backup", backup)
    return demo, calls


def test_upgrade_preserves_inputs_identity_and_repeats_without_migration(prepared):
    demo, calls = prepared
    original = {n: (demo.path / n).read_bytes() for n in module.PRIVATE}
    module.upgrade(demo, Path("web"))
    assert original == {n: (demo.path / n).read_bytes() for n in module.PRIVATE}
    assert demo.state["env"]["DEMO_API_PORT"] == "18080"
    assert demo.state["env"]["DEMO_WEB_PORT"] == "18081"
    assert demo.state["version"] == 14
    assert (demo.path / "backups/owned/database.dump").exists()
    assert not (demo.path / "upgrade.pending.json").exists()
    assert calls.index(("backup",)) < next(
        i for i, c in enumerate(calls) if "alembic" in c
    )
    assert not any("bootstrap" in c or "down" in c or "--volumes" in c for c in calls)
    calls.clear()
    module.upgrade(demo, Path("web"))
    assert calls == [("verify",)]


def test_interrupted_migration_is_resumable_without_bootstrap_or_new_secrets(prepared):
    demo, calls = prepared
    dc = demo.dc
    fail = True

    def interrupted(*args):
        nonlocal fail
        dc(*args)
        if "alembic" in args and fail:
            fail = False
            raise RuntimeError("interrupted")

    demo.dc = interrupted
    with pytest.raises(RuntimeError):
        module.upgrade(demo, Path("web"))
    assert (demo.path / "upgrade.pending.json").exists()
    assert demo.state["web_revision"] == "old"
    module.upgrade(demo, Path("web"))
    assert demo.state["web_revision"] == "new"
    assert calls.count(("backup",)) == 1


def test_interrupted_after_metadata_switch_resumes_same_plan(prepared):
    demo, _ = prepared
    verify = demo.verify
    demo.verify = lambda: (_ for _ in ()).throw(RuntimeError("lost verification"))
    with pytest.raises(RuntimeError):
        module.upgrade(demo, Path("web"))
    assert demo.state["web_revision"] == "new"
    demo.verify = verify
    module.upgrade(demo, Path("web"))
    assert not (demo.path / "upgrade.pending.json").exists()


def test_changed_private_input_prevents_resume(prepared):
    demo, calls = prepared
    demo.verify = lambda: (_ for _ in ()).throw(RuntimeError("interrupted"))
    with pytest.raises(RuntimeError):
        module.upgrade(demo, Path("web"))
    (demo.path / "mqtt/server.key").write_text("different")
    calls.clear()
    with pytest.raises(ValueError):
        module.upgrade(demo, Path("web"))
    assert calls == []


def test_missing_web_fails_before_stopping_any_service(tmp_path):
    demo = SimpleNamespace(step=None, guard=lambda: None)
    with pytest.raises(ValueError):
        module.check(demo, tmp_path / "absent")


def test_parallel_upgrade_is_rejected_and_lock_releases(prepared):
    demo, _ = prepared
    with module.operation_lock(demo):
        with pytest.raises(ValueError):
            module.upgrade(demo, Path("web"))
    module.upgrade(demo, Path("web"))


def test_changed_candidate_image_prevents_resume(prepared, monkeypatch):
    demo, calls = prepared
    demo.verify = lambda: (_ for _ in ()).throw(RuntimeError("interrupted"))
    with pytest.raises(RuntimeError):
        module.upgrade(demo, Path("web"))
    monkeypatch.setattr(module, "image_ids", lambda env: {"fixture": "different"})
    calls.clear()
    with pytest.raises(ValueError):
        module.upgrade(demo, Path("web"))
    assert calls == []


@pytest.mark.parametrize("fault", ["remote-db", "tls-disabled"])
def test_incompatible_configuration_fails_before_mutation(tmp_path, monkeypatch, fault):
    web = tmp_path / "web"
    for name in ("src/data/core/permissions.ts", "src/data/core/relay-control.ts"):
        target = web / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("reviewed-layout")
    for name in (*module.PRIVATE, "state.json", "compose.env"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("preserved")
    monkeypatch.setattr(module, "check_web", lambda path: None)
    values = {
        "DATABASE_URL": f"postgresql+psycopg://kzhome:{'a' * 64}@postgres:5432/kzhome",
        "MQTT_TLS_ENABLED": "true",
        "MQTT_HOST": "mosquitto",
        "APP_ENV": "production",
    }
    values["DATABASE_URL" if fault == "remote-db" else "MQTT_TLS_ENABLED"] = (
        "incompatible"
    )
    calls = []

    def dc(*args):
        calls.append(args)
        return json.dumps({"services": {"core": {"environment": values}}})

    demo = SimpleNamespace(
        path=tmp_path,
        state={"application_password": "a" * 64},
        guard=lambda: None,
        dc=dc,
    )
    with pytest.raises(ValueError):
        module.check(demo, web)
    assert calls == [("config", "--format", "json")]


@pytest.mark.parametrize(
    "mode,ack,state",
    [("apply", "applied", True), ("fail", "failed", False), ("timeout", None, False)],
)
def test_virtual_relay_preserves_ids_and_never_fakes_success(mode, ack, state):
    published = AsyncMock()
    relay = Relay("e2e_house", "e2e_relay", published)
    command = CommandEnvelope(
        command_id="command",
        correlation_id="correlation",
        timestamp=utc_now(),
        state={"on": True},
    )
    asyncio.run(
        handle(
            relay,
            "kzhome/v1/e2e_house/e2e_relay/set",
            command.model_dump_json().encode(),
            False,
            mode,
        )
    )
    assert relay.on is state
    if ack:
        value = json.loads(published.call_args_list[0].args[1])
        assert (
            value["command_id"] == "command"
            and value["correlation_id"] == "correlation"
        )
        assert value["status"] == ack
    else:
        published.assert_not_called()
