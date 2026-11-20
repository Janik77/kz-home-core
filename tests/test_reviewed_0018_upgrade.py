"""The exact #60 schema0018 source is a copy, not a migration."""
from contextlib import closing
import hashlib
import json
import sqlite3
import subprocess

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine

from tools import durable_software as manager
from tools.restore_policy import full_fingerprints


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def owned_backup(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    sqlite = backup / "database.dump"
    engine = create_engine(f"sqlite:///{sqlite}")
    with engine.begin() as con:
        cfg = Config()
        cfg.set_main_option("script_location", str(manager.CORE / "alembic"))
        cfg.attributes["connection"] = con
        command.upgrade(cfg, manager.EXPECTED_MIGRATION)
        for sql in (
            "INSERT INTO houses(id,name,metadata_revision) VALUES('h','Preserved home',4)",
            "INSERT INTO floors(id,house_id,name,\"order\") VALUES('f','h','Floor',0)",
            "INSERT INTO rooms(id,floor_id,name) VALUES('r','f','Preserved room')",
            "INSERT INTO devices(id,room_id,name,type,state,capabilities,metadata,online) VALUES('d','r','Relay','relay','{\"on\":true}','[\"on_off\"]','{}',true)",
            "INSERT INTO users(id,email,password_hash,is_active,is_superuser) VALUES('u','owner@example.test','placeholder',true,false)",
            "INSERT INTO house_memberships(id,user_id,house_id,role) VALUES('m','u','h','owner')",
            "INSERT INTO device_favorites(user_id,house_id,device_id) VALUES('u','h','d')",
            "INSERT INTO scenes(id,name,house_id,actions,is_draft) VALUES('s','Preserved draft','h','[]',true)",
            "INSERT INTO event_logs(id,house_id,event_type,entity_id,payload,correlation_id) VALUES('e','h','synthetic','d','{}','e')",
        ):
            con.exec_driver_sql(sql)
        before = full_fingerprints(con)
    engine.dispose()
    for name in manager.FILES:
        (backup / name).write_text("synthetic")
    profile = {
        "owner": "a" * 32,
        "profile_path": str(tmp_path / "protected-source"),
        "directory_identity": "10:20",
        "database_identity": "1234",
        "web_port": 39225,
        "core_sha": manager.SOURCE60_PROFILE[0],
        "web_sha": manager.SOURCE60_PROFILE[1],
    }
    (backup / "profile.json").write_text(json.dumps(profile))
    manifest = {
        "format": 2,
        "sha256": {p.name: digest(p) for p in backup.iterdir()},
        "database": before,
    }
    (backup / "manifest.json").write_text(json.dumps(manifest))
    return backup, manifest


@pytest.mark.parametrize("wrong", ["HEAD", "80770e8a", "f" * 40])
def test_source_commit_must_be_complete_exact_hash(wrong):
    with pytest.raises(ValueError, match="exact Core #60"):
        manager.reviewed_0018_source(wrong)


def test_source_commit_is_verified_by_object_without_replacements(monkeypatch):
    args = []

    def git(command, **kwargs):
        args.extend(command)
        assert "--no-replace-objects" in command
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(manager.subprocess, "check_output", git)
    with pytest.raises(ValueError, match="absent"):
        manager.reviewed_0018_source(manager.SOURCE60_TOOL_SHA)
    assert args[-1] == manager.SOURCE60_TOOL_SHA + "^{commit}"


@pytest.mark.parametrize("bad", ["format", "missing", "extra", "checksum", "stored-sha", "physical"])
def test_bad_source_rejected_before_destination(owned_backup, tmp_path, monkeypatch, bad):
    backup, manifest = owned_backup
    if bad == "format":
        manifest["format"] = 1
    elif bad == "missing":
        del manifest["database"]["device_favorites"]
    elif bad == "extra":
        manifest["database"]["unexpected"] = {"rows": 0, "sha256": "a" * 64}
    elif bad == "checksum":
        manifest["sha256"]["database.dump"] = "f" * 64
    elif bad == "physical":
        manifest["database"]["physical_devices"]["rows"] = 1
    elif bad == "stored-sha":
        p = backup / "profile.json"
        state = json.loads(p.read_text())
        state["core_sha"] = manager.SOURCE60_TOOL_SHA
        p.write_text(json.dumps(state))
        manifest["sha256"][p.name] = digest(p)
    (backup / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(manager, "Docker", lambda: pytest.fail("Must fail before Docker"))
    destination = tmp_path / "destination"
    with pytest.raises(ValueError):
        manager._restore(backup, destination, upgrading=True,
                         source_version=manager.SOURCE60_PROFILE, same_schema=True)
    assert not destination.exists()


@pytest.mark.parametrize("mutation", [None, "changed-data", "wrong-schema"])
def test_same_schema_restores_fresh_copy_no_ddl_and_original_untouched(
    owned_backup, tmp_path, monkeypatch, mutation
):
    backup, manifest = owned_backup
    before_files = {p.name: digest(p) for p in backup.iterdir()}
    dest = tmp_path / "destination"
    db = dest / "owned-test.sqlite"
    stop = []
    monkeypatch.setattr(manager, "CORE_SHA", manager.CORE61_RUNTIME_SHA)
    monkeypatch.setattr(manager, "WEB_SHA", manager.CORE61_WEB_SHA)
    monkeypatch.setattr(manager, "migrate_upgrade", lambda **_: pytest.fail("No Alembic DDL"))

    class FakeDocker:
        def text(self, *_):
            return "0"

        def run(self, *command_args, data):
            assert "pg_restore" in command_args
            assert data == (backup / "database.dump").read_bytes()
            with closing(sqlite3.connect(backup / "database.dump")) as source:
                with closing(sqlite3.connect(db)) as target:
                    source.backup(target)
                    if mutation == "changed-data":
                        target.execute("UPDATE rooms SET name='Unexpected next state'")
                    elif mutation == "wrong-schema":
                        target.execute("UPDATE alembic_version SET version_num='0017_metadata_cas'")
                    target.commit()

    class Copy:
        def __init__(self, path, docker):
            self.path, self.docker, self.name = path, docker, "synthetic-copy"
            self.state = json.loads((path / "profile.json").read_text())

        def create_storage(self):
            assert not db.exists()

        def _open_pg(self):
            return f"sqlite:///{db}"

        def save_identity(self):
            self.state["database_identity"] = "5678"
            (self.path / "profile.json").write_text(json.dumps(self.state))

        def query(self, sql):
            with closing(sqlite3.connect(db)) as connection:
                return connection.execute(sql).fetchone()[0]

        def verify_database(self):
            assert self.query("SELECT version_num FROM alembic_version") == manager.EXPECTED_MIGRATION

        def stop(self):
            stop.append(self.path)

    monkeypatch.setattr(manager, "Docker", FakeDocker)
    monkeypatch.setattr(manager, "Profile", Copy)

    def run():
        return manager._restore(backup, dest, upgrading=True,
                                source_version=manager.SOURCE60_PROFILE, same_schema=True)

    if mutation:
        with pytest.raises(ValueError, match="fingerprints|Unsupported source migration"):
            run()
        assert not (dest / "initialized").exists()
        assert not (dest / "seeded").exists()
    else:
        target = run()
        assert target.state["owner"] != "a" * 32
        assert target.state["web_port"] != 39225
        assert target.state["database_identity"] == "5678"
        assert target.state["core_sha"] == manager.CORE61_RUNTIME_SHA
        assert target.state["web_sha"] == manager.CORE61_WEB_SHA
        assert target.state["profile_path"] == str(dest)
        assert (dest / "initialized").is_file()
        assert (dest / "seeded").is_file()
        policy = json.loads((dest / "restore-policy.json").read_text())
        assert policy["before"] == manifest["database"] == policy["after"]
        assert policy["changes"] == []
    assert stop == [dest]
    assert {p.name: digest(p) for p in backup.iterdir()} == before_files
