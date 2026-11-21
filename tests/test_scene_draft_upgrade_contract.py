"""Full pre/post-DDL preservation for schema0017→0018, with no services."""

from pathlib import Path
from unittest.mock import Mock
import subprocess
import sys
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from tools import durable_software as manager
from tools.restore_policy import database_snapshot, verified_scene_draft_migration


def test_exact_source60_uses_old_runtime_and_full_same_schema_backup():
    assert manager.SOURCE60_TOOL_SHA == "80770e8a3f59d0bd7fb825345f0ca7589dd26f23"
    assert manager.SUPPORTED_UPGRADE_SOURCES[manager.SOURCE60_TOOL_SHA] == (
        "1a43ecba1cd07dd231f313fcd42d4d4276e13967",
        "36515bdcb003e875108dd722febe5c969825f304",
        "0018_scene_drafts",
    )
    assert "0018_scene_drafts" in manager.FORMAT_2_SOURCE_MIGRATIONS
    assert "0015_house_schedules" not in manager.FORMAT_2_SOURCE_MIGRATIONS


def test_source58_registry_and_no_metadata_or_wait_exclusions():
    assert manager.SUPPORTED_UPGRADE_SOURCES[manager.SOURCE58_TOOL_SHA] == (
        "fc232035f78c99171fd72ac82c78eb1b5bb657fa",
        "31dd1029173af6c7ff4ed4e9bf0c80679c185b3b",
        "0017_metadata_cas",
    )
    p = Mock()
    p.query.side_effect = [
        "houses\nscene_run_waits",
        "id\nmetadata_revision",
        "a",
        "run_id\ndue_at",
        "b",
    ]
    assert manager.legacy_fingerprints(p, "0017_metadata_cas") == {
        "houses": "a",
        "scene_run_waits": "b",
    }
    queries = [call.args[0] for call in p.query.call_args_list]
    assert "'scene_run_waits'" not in queries[0]
    assert "'metadata_revision'" not in queries[1]
    assert "'is_draft'" in queries[1]
    assert '"metadata_revision"' in queries[2]
    assert '"due_at"' in queries[4]


@pytest.mark.parametrize("source", ["0016_durable_waits", "0017_metadata_cas"])
@pytest.mark.parametrize("mutation", [None, "name", "counter", "draft", "table"])
def test_exact_allowed_draft_migration_delta(tmp_path, source, mutation):
    engine = create_engine(f"sqlite:///{tmp_path / 'draft-upgrade.db'}")
    with engine.begin() as conn:
        cfg = Config()
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, source)
        conn.execute(text("INSERT INTO houses(id,name) VALUES('h','Keep user name')"))
        conn.execute(
            text(
                "INSERT INTO scenes(id,name,house_id,actions) VALUES('s','Existing active','h','[]')"
            )
        )
        if source == "0017_metadata_cas":
            conn.execute(text("UPDATE houses SET metadata_revision=7 WHERE id='h'"))
        tables, rows, before = database_snapshot(conn)
        command.upgrade(cfg, "0018_scene_drafts")
        if mutation == "name":
            conn.execute(text("UPDATE houses SET name='lost data' WHERE id='h'"))
        elif mutation == "counter":
            conn.execute(text("UPDATE houses SET metadata_revision=2 WHERE id='h'"))
        elif mutation == "draft":
            conn.execute(text("UPDATE scenes SET is_draft=true WHERE id='s'"))
        elif mutation == "table":
            conn.execute(text("CREATE TABLE unexpected(id INTEGER)"))
        if mutation:
            with pytest.raises(ValueError, match="Scene draft migration changed"):
                verified_scene_draft_migration(conn, tables, rows, source)
        else:
            expected = verified_scene_draft_migration(conn, tables, rows, source)
            assert expected == database_snapshot(conn)[2]
            assert expected["scene_run_waits"] == before["scene_run_waits"]
            assert conn.scalar(
                text("SELECT metadata_revision FROM houses WHERE id='h'")
            ) == (7 if source == "0017_metadata_cas" else 1)
            assert conn.scalar(text("SELECT is_draft FROM scenes WHERE id='s'")) == 0
    engine.dispose()


def test_source58_manifest_required_before_mutating(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'unchanged.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        cfg = Config()
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0017_metadata_cas")
        before = database_snapshot(conn)[2]
    p = Mock()
    p._open_pg.return_value = url
    p.query.return_value = "0"
    monkeypatch.setattr(manager, "legacy_fingerprints", lambda *args: {"same": "rows"})
    with pytest.raises(ValueError, match="requires full backup fingerprints"):
        manager.migrate_upgrade(p, "0017_metadata_cas")
    with pytest.raises(ValueError, match="Source backup database fingerprint mismatch"):
        manager.migrate_upgrade(
            p, "0017_metadata_cas", expected_source={"false": "manifest"}
        )
    with engine.connect() as conn:
        assert database_snapshot(conn)[2] == before
    engine.dispose()


def test_existing_launcher_is_cwd_independent_and_exposes_repeat_start():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-B", str(root / "tools/durable_software.py"), "--help"],
        cwd=root / "tests",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert (
        "--state" in result.stdout
        and "--web-root" in result.stdout
        and "start" in result.stdout
    )
