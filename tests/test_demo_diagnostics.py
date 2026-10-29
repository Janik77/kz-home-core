"""Windows preparation failures: disposable files, no Docker or secret output."""

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from deploy import demo

SENTINEL = "PRIVATE-password-token-file-content"


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    root = tmp_path / "Core space"
    for name in (
        "compose.production.yaml", "compose.demo.yaml", "compose.arm64.yaml",
        "deploy/demo-web.Dockerfile", "deploy/nginx/arm64.conf",
        "deploy/mosquitto/acl", "deploy/mosquitto/start.sh",
        "deploy/mosquitto/mosquitto.conf",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(demo.ROOT / name, target)
    web = tmp_path / "Web space"
    web.mkdir()
    files = ["package.json", "package-lock.json", "index.html", "Фото комнаты.txt"]
    for name in files:
        (web / name).write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(demo, "ROOT", root)
    # Exercise Windows commands on every test OS without changing os.name globally.
    monkeypatch.setattr(demo, "os", SimpleNamespace(name="nt", environ={}))
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if "--show-toplevel" in args:
            return str(web)
        if "ls-files" in args:
            assert "-z" in args
            return "\0".join(files) + "\0"
        if args[0] == "whoami":
            return '"fixture-user","S-1-5-21-123-456-789-1001"'
        if args[:2] == ["docker", "info"]:
            return "linux"
        if args[:3] == ["docker", "compose", "version"]:
            return "2.39.1"
        if "rev-parse" in args:
            return "fixture-revision"
        return ""

    monkeypatch.setattr(demo, "command", command)
    monkeypatch.setattr(demo.Demo, "guard", lambda self: None)
    return root, web, command, calls


def run_prepare(monkeypatch, web, *extra):
    monkeypatch.setattr("sys.argv", ["demo", "prepare", "--run", "--web", str(web), *extra])
    demo.main()


@pytest.mark.parametrize("stage,match", [
    ("web-checkout", lambda a: "--show-toplevel" in a),
    ("docker-engine", lambda a: a[:2] == ["docker", "info"]),
    ("compose-version", lambda a: a[:3] == ["docker", "compose", "version"]),
    ("windows-sid", lambda a: a[0] == "whoami"),
    ("windows-acl", lambda a: a[0] == "icacls"),
    ("mqtt-certificates", lambda a: "alpine/openssl" in a),
    ("mqtt-passwords", lambda a: "mosquitto_passwd" in a),
])
def test_prepare_reports_failed_stage_without_subprocess_secrets(
    preparation, monkeypatch, capsys, stage, match
):
    root, web, original, _ = preparation

    def fail(args, **kwargs):
        if match(args):
            raise subprocess.CalledProcessError(7, [SENTINEL], SENTINEL, SENTINEL)
        return original(args, **kwargs)

    monkeypatch.setattr(demo, "command", fail)
    with pytest.raises(SystemExit) as result:
        run_prepare(monkeypatch, web)
    assert result.value.code == 1
    output = capsys.readouterr().out
    assert f"FAIL [{stage}]" in output and "code 7" in output
    assert SENTINEL not in output
    private = root / "deploy/local/demo/kzhome-demo"
    if stage in {"web-checkout", "docker-engine", "compose-version"}:
        assert not private.exists()
    else:
        assert private.exists() and not (private / "state.json").exists()
        # The second attempt must identify partial preparation, never overwrite it.
        before = {p.relative_to(private): p.read_bytes() for p in private.rglob("*") if p.is_file()}
        with pytest.raises(SystemExit):
            run_prepare(monkeypatch, web)
        assert "FAIL [incomplete-prepare]" in capsys.readouterr().out
        assert before == {p.relative_to(private): p.read_bytes() for p in private.rglob("*") if p.is_file()}


@pytest.mark.parametrize("fault,code", [
    ("missing-web", "web-checkout"), ("missing-lock", "web-files"),
    ("untracked-lock", "web-files"), ("dirty", "web-dirty"),
    ("git-parent", "web-checkout"), ("old-compose", "compose-version"),
    ("windows-engine", "docker-engine"), ("missing-core", "core-assets"),
    ("invalid-port", "arguments"),
])
def test_preflight_failure_creates_no_private_directory(
    preparation, monkeypatch, capsys, fault, code
):
    root, web, original, _ = preparation
    if fault == "missing-web":
        web = web / "absent"
    if fault == "missing-lock":
        (web / "package-lock.json").rename(web / "saved-lock")
    if fault == "missing-core":
        (root / "compose.demo.yaml").rename(root / "saved-compose")

    def command(args, **kwargs):
        if fault == "dirty" and "status" in args:
            return " M " + SENTINEL
        if fault == "git-parent" and "--show-toplevel" in args:
            return str(web.parent)
        if fault == "untracked-lock" and "ls-files" in args:
            return "package.json\0index.html\0"
        if fault == "old-compose" and "--short" in args:
            return "2.24.3"
        if fault == "windows-engine" and "info" in args:
            return "windows"
        return original(args, **kwargs)

    monkeypatch.setattr(demo, "command", command)
    with pytest.raises(SystemExit):
        run_prepare(monkeypatch, web, *(["--api-port", "80"] if fault == "invalid-port" else []))
    output = capsys.readouterr().out
    assert f"FAIL [{code}]" in output and SENTINEL not in output
    assert not (root / "deploy/local/demo/kzhome-demo").exists()


def test_success_and_repeat_preserve_inputs_and_unicode_snapshot(preparation, monkeypatch, capsys):
    root, web, _, calls = preparation
    run_prepare(monkeypatch, web)
    private = root / "deploy/local/demo/kzhome-demo"
    assert (private / "web/Фото комнаты.txt").read_text(encoding="utf-8") == "fixture"
    assert (web / "Фото комнаты.txt").read_text(encoding="utf-8") == "fixture"
    state = json.loads((private / "state.json").read_text(encoding="utf-8"))
    assert state["env"]["DEMO_INPUT_DIR"] == str(private).replace("\\", "/")
    assert any(a[0] == "icacls" and "/inheritance:r" in a for a in calls)
    for args in calls:
        if "--mount" in args:
            assert args[args.index("--mount") + 1] == f"type=bind,source={private / 'mqtt'},target=/bootstrap"
    before = (private / "state.json").read_bytes(), (private / "user.json").read_bytes()
    count = len(calls)
    run_prepare(monkeypatch, web)
    assert len(calls) == count
    assert before == ((private / "state.json").read_bytes(), (private / "user.json").read_bytes())
    output = capsys.readouterr().out
    for value in (state["application_password"], state["env"]["AUTH_JWT_SECRET"], state["env"]["MQTT_PASSWORD"]):
        assert value not in output


@pytest.mark.parametrize("error,reason", [
    (FileNotFoundError(SENTINEL), "not found"),
    (PermissionError(SENTINEL), "access denied"),
    (subprocess.TimeoutExpired([SENTINEL], 1, SENTINEL, SENTINEL), "timed out"),
    (ValueError(SENTINEL), "validation or operation failed"),
    (KeyboardInterrupt(SENTINEL), "interrupted"),
])
def test_unexpected_errors_are_redacted(preparation, monkeypatch, capsys, error, reason):
    _, web, _, _ = preparation

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(demo, "command", fail)
    with pytest.raises(SystemExit):
        run_prepare(monkeypatch, web)
    output = capsys.readouterr().out
    assert reason in output and SENTINEL not in output


def test_corrupt_existing_metadata_is_not_exposed_or_overwritten(preparation, monkeypatch, capsys):
    root, web, _, calls = preparation
    private = root / "deploy/local/demo/kzhome-demo"
    private.mkdir(parents=True)
    state = private / "state.json"
    state.write_text('{"password":"' + SENTINEL, encoding="utf-8")
    before = state.read_bytes()
    with pytest.raises(SystemExit):
        run_prepare(monkeypatch, web)
    output = capsys.readouterr().out
    assert "FAIL [existing-config]" in output and SENTINEL not in output
    assert state.read_bytes() == before and calls == []


def test_command_uses_utf8_and_hides_failed_output(monkeypatch):
    def run(args, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        return subprocess.CompletedProcess(args, 3, SENTINEL, SENTINEL)

    monkeypatch.setattr(demo.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError) as error:
        demo.command(["git", "status"])
    assert error.value.stdout is None and error.value.stderr is None
