import json

import pytest

from tools.durable_software import (
    CORE_SHA,
    WEB_SHA,
    LABEL,
    Profile,
    new_directory,
    restore,
)


class ForeignDocker:
    def run(self, *args, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps(
                [{"Config": {"Labels": {LABEL: "another-owner"}}}]
            ).encode()

        return Result()


def test_foreign_container_is_never_adopted(tmp_path):
    (tmp_path / "profile.json").write_text(
        json.dumps(
            {
                "owner": "a" * 32,
                "core_sha": CORE_SHA,
                "web_sha": WEB_SHA,
                "profile_path": str(tmp_path),
            }
        )
    )
    profile = Profile(tmp_path, ForeignDocker())
    with pytest.raises(ValueError, match="foreign"):
        profile.owned("container", profile.name + "-pg")


def test_new_directory_never_overwrites_or_follows_symlinks(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    sentinel = target / "keep"
    sentinel.write_text("untouched")
    with pytest.raises(FileExistsError):
        new_directory(target)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        new_directory(link / "new")
    assert sentinel.read_text() == "untouched"


def test_restore_rejects_unexpected_manifest_before_docker(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "manifest.json").write_text(
        json.dumps({"format": 1, "sha256": {"../escape": "bad"}})
    )
    with pytest.raises(ValueError, match="manifest"):
        restore(backup, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_restore_rejects_symlink_manifest_before_docker(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    other = tmp_path / "manifest"
    other.write_text("{}")
    (backup / "manifest.json").symlink_to(other)
    with pytest.raises(ValueError, match="Symlink"):
        restore(backup, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize(
    "host,system",
    [
        ("ssh://orchestrator", "Docker Desktop"),
        ("unix:///var/run/docker.sock", "Ubuntu 24.04"),
    ],
)
def test_reject_remote_or_native_vm_without_ci(monkeypatch, host, system):
    from types import SimpleNamespace
    from tools.durable_software import Docker

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def context(*args, **kwargs):
        command = args[0]
        content = (
            "default"
            if command[-1] == "show"
            else json.dumps([{"Endpoints": {"docker": {"Host": host}}}])
        )
        return SimpleNamespace(stdout=content)

    monkeypatch.setattr("tools.durable_software.subprocess.run", context)
    monkeypatch.setattr(Docker, "text", lambda *args: system)
    with pytest.raises(ValueError):
        Docker()


def test_copied_profile_rejected_before_docker(tmp_path):
    (tmp_path / "profile.json").write_text(
        json.dumps(
            {
                "owner": "a" * 32,
                "core_sha": CORE_SHA,
                "web_sha": WEB_SHA,
                "profile_path": str(tmp_path / "original"),
            }
        )
    )
    with pytest.raises(ValueError, match="directory binding"):
        Profile(tmp_path, ForeignDocker())


def test_missing_volume_backup_does_not_create_or_stop(tmp_path, monkeypatch):
    (tmp_path / "profile.json").write_text(
        json.dumps(
            {
                "owner": "a" * 32,
                "core_sha": CORE_SHA,
                "web_sha": WEB_SHA,
                "profile_path": str(tmp_path),
                "database_identity": "123",
            }
        )
    )
    (tmp_path / "seeded").write_text("1")
    profile = Profile(tmp_path, ForeignDocker())
    monkeypatch.setattr(profile, "owned", lambda *args: None)

    def forbidden(*args):
        pytest.fail("Lost-volume path mutated resources")

    monkeypatch.setattr(profile, "stop", forbidden)
    monkeypatch.setattr(profile.docker, "run", forbidden)
    with pytest.raises(ValueError, match="storage missing"):
        profile.backup(tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
