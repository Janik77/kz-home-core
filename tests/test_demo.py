"""Offline demo provisioning safety and unchanged real service semantics."""

import json
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.db import Base
from app.demo import bootstrap_demo
from app.models import UserORM, HouseORM, DeviceORM, SceneORM, AutomationORM
from app.security import PasswordManager
from deploy.demo import project_path, proxy_config


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_demo_bootstrap_repeat_preserves_password_state_and_disabled_rule(engine):
    email = "demo-unit@example.invalid"
    password = "unit-only-test-input"
    bootstrap_demo(engine, email, password)
    with Session(engine) as session:
        user = session.scalar(select(UserORM))
        original_hash = user.password_hash
        assert PasswordManager().verify(original_hash, password)
        assert not user.is_superuser
        assert session.get(DeviceORM, "e2e_relay").metadata_["simulated"]
        assert session.scalar(select(func.count()).select_from(SceneORM)) == 2
        relay = session.get(DeviceORM, "e2e_relay")
        relay.state = {"on": True}
        session.commit()
    bootstrap_demo(engine, email, password)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(UserORM)) == 1
        assert session.scalar(select(UserORM)).password_hash == original_hash
        assert session.get(DeviceORM, "e2e_relay").state == {"on": True}
        assert not session.get(AutomationORM, "e2e_motion_relay").enabled


def test_demo_rejects_foreign_account_and_does_not_change_it(engine):
    bootstrap_demo(engine, "demo-owner@example.invalid", "unit-only-test-input")
    with pytest.raises(ValueError):
        bootstrap_demo(engine, "demo-other@example.invalid", "unit-only-test-input")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(UserORM)) == 1
        assert session.get(HouseORM, "e2e_house").name.startswith("DEMO")


def test_demo_rejects_non_demo_email_before_writes(engine):
    with pytest.raises(ValueError):
        bootstrap_demo(engine, "real@example.invalid", "unit-only-test-input")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(UserORM)) == 0


@pytest.mark.parametrize(
    "project",
    ["kzhome", "kzhome-local", "kzhome-arm64", "../escape", "kzhome-demo/../../escape"],
)
def test_demo_refuses_installation_names_and_path_escape(project):
    with pytest.raises(ValueError):
        project_path(project)


def test_demo_proxy_reuses_reviewed_routes_without_tls_or_public_bindings():
    source = (
        Path(__file__).resolve().parents[1] / "deploy/nginx/arm64.conf"
    ).read_text()
    result = proxy_config(source)
    assert "listen 8080;" in result and "listen 443" not in result
    assert "ssl_certificate" not in result
    assert "proxy_next_upstream off;" in result
    assert "limit_req zone=kzhome_auth" in result
    assert "proxy_pass http://kzhome_core/auth/;" in result
    assert "proxy_pass http://kzhome_core/;" in result


def test_demo_tool_opt_in_never_reads_credentials_without_authorization(
    monkeypatch, capsys
):
    from app.demo import main

    monkeypatch.delenv("KZHOME_DEMO_ENABLED", raising=False)
    monkeypatch.setattr("logging.disable", lambda level: None)
    monkeypatch.setattr("sys.argv", ["demo", "bootstrap"])
    with pytest.raises(SystemExit):
        main()
    assert "details withheld" in capsys.readouterr().out


def test_preparation_repeat_preserves_private_files(tmp_path, monkeypatch):
    from deploy.demo import Demo

    demo = Demo("kzhome-demo-unit")
    demo.path = tmp_path
    state = {"version": 14, "project": demo.project, "env": {}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    (tmp_path / "user.json").write_text('{"password":"test-only"}')
    monkeypatch.setattr(demo, "guard", lambda: None)
    demo.prepare(Path("absent"), "linux/amd64", 18080, 18081)
    assert (tmp_path / "user.json").read_text() == '{"password":"test-only"}'


def test_web_snapshot_excludes_environment_and_preserves_source(tmp_path, monkeypatch):
    from deploy.demo import stage_web

    source = tmp_path / "source"
    source.mkdir()
    for filename in ("package.json", "package-lock.json", "index.html", ".env.local"):
        (source / filename).write_text("local-test-input")

    def git(args, **kwargs):
        if "status" in args:
            return ""
        if "--show-toplevel" in args:
            return str(source)
        return (
            "package.json\0package-lock.json\0index.html\0.env.local\0"
            if "ls-files" in args
            else "test-revision"
        )

    monkeypatch.setattr("deploy.demo.command", git)
    target = tmp_path / "snapshot"
    stage_web(source, target)
    assert (source / ".env.local").read_text() == "local-test-input"
    assert not (target / ".env.local").exists()
    assert (target / "Dockerfile").exists()


@pytest.mark.skipif(
    os.environ.get("RUN_DEMO_CONFIG_TESTS") != "1",
    reason="explicit Docker Compose config opt-in",
)
@pytest.mark.parametrize("arm64", [False, True])
def test_demo_effective_compose_isolated_and_secure(tmp_path, arm64):
    root = Path(__file__).resolve().parents[1]
    envfile = tmp_path / "dummy.env"
    envfile.write_text("# No actual installation inputs\n")
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": "validation-only",
        "MQTT_PASSWORD": "validation-only",
        "AUTH_JWT_SECRET": "validation-only",
        "DATABASE_URL": "postgresql+psycopg://dummy:dummy@postgres/kzhome",
        "DEMO_CORE_IMAGE": "demo-validation-core",
        "DEMO_WEB_IMAGE": "demo-validation-web",
        "DEMO_INPUT_DIR": str(tmp_path),
        "DEMO_WEB_CONTEXT": str(tmp_path),
        "DEMO_PLATFORM": "linux/arm64" if arm64 else "linux/amd64",
        "DEMO_API_PORT": "18080",
        "DEMO_WEB_PORT": "18081",
        "HTTPS_BIND_IP": "127.0.0.1",
        "MQTT_ACL_FILE": str(tmp_path / "acl"),
    }
    args = [
        "docker",
        "compose",
        "-p",
        "kzhome-demo-config-check",
        "--env-file",
        str(envfile),
        "-f",
        str(root / "compose.production.yaml"),
    ]
    if arm64:
        args += ["-f", str(root / "compose.arm64.yaml")]
    args += ["-f", str(root / "compose.demo.yaml"), "config", "--format", "json"]
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    config = json.loads(result.stdout)
    assert set(config["services"]) == {"core", "postgres", "mosquitto", "proxy"}
    assert not config["services"]["postgres"].get("ports")
    assert not config["services"]["mosquitto"].get("ports")
    core = config["services"]["core"]
    assert core["environment"]["APP_ENV"] == "production"
    assert core["environment"]["MQTT_TLS_ENABLED"] == "true"
    assert core["environment"]["KZHOME_SIMULATOR_ENABLED"] == "false"
    assert config["networks"]["backend"]["internal"]
    for name, service in config["services"].items():
        assert service["platform"] == env["DEMO_PLATFORM"]
        assert all(p["host_ip"] == "127.0.0.1" for p in service.get("ports", []))
        assert not service.get("container_name")
    assert len(config["services"]["proxy"]["volumes"]) == 1
    assert len(config["services"]["proxy"]["ports"]) == 1
    assert config["services"]["proxy"]["ports"][0]["target"] == 8080
    assert all(
        v["name"].startswith("kzhome-demo-config-check_") and not v.get("external")
        for v in config["volumes"].values()
    )
