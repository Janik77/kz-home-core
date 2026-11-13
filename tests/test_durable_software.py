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
        json.dumps({"owner": "a" * 32, "core_sha": CORE_SHA, "web_sha": WEB_SHA})
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
