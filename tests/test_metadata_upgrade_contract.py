"""Source56 preservation contract; actual profiles are qualified only in CI."""

from pathlib import Path
from unittest.mock import Mock
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text, inspect
from tools import durable_software as manager


def test_source56_exact_contract_and_wait_columns_preserved():
    assert manager.SUPPORTED_UPGRADE_SOURCES[manager.SOURCE56_TOOL_SHA] == (
        "af6dcadddcfc2d5bee97da4386021495a54d1f9a",
        "2ccf4172579563ad5e6cb1b0f4dba4147aada558",
        "0016_durable_waits",
    )
    profile = Mock()
    profile.query.side_effect = [
        "scene_runs\nscene_run_waits",
        "id\nwait_position\ncancelled_at",
        "a",
        "run_id\nposition\ndue_at",
        "b",
    ]
    assert manager.legacy_fingerprints(profile, "0016_durable_waits") == {
        "scene_runs": "a",
        "scene_run_waits": "b",
    }
    queries = [c.args[0] for c in profile.query.call_args_list]
    assert "'scene_run_waits'" not in queries[0]
    assert "wait_position" not in queries[1]  # No exclusion of existing waits.
    assert "metadata_revision" in queries[1]
    assert '"wait_position"' in queries[2]
    assert '"due_at"' in queries[4]


def test_oldest_source_room_floor_columns_are_not_broadly_excluded():
    p = Mock()
    p.query.side_effect = ["floors\nrooms", "a", "b"]
    assert manager.legacy_fingerprints(p) == {"floors": "a", "rooms": "b"}
    queries = [c.args[0] for c in p.query.call_args_list]
    assert 'id, house_id, name, "order", created_at, updated_at' in queries[1]
    assert "id, floor_id, name, icon, created_at, updated_at" in queries[2]
    assert "metadata_revision" not in queries[1] + queries[2]


def test_false_source56_manifest_is_refused_before_any_migration(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'source56.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        cfg = Config()
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0016_durable_waits")
    profile = Mock()
    profile._open_pg.return_value = url
    profile.query.return_value = "0"
    monkeypatch.setattr(
        manager, "legacy_fingerprints", lambda *args: {"sentinel": "unchanged"}
    )
    with pytest.raises(ValueError, match="requires full backup fingerprints"):
        manager.migrate_upgrade(profile, "0016_durable_waits")
    with pytest.raises(ValueError, match="Source backup database fingerprint mismatch"):
        manager.migrate_upgrade(
            profile, "0016_durable_waits", expected_source={"false": "manifest"}
        )
    with engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0016_durable_waits"
        )
        assert "metadata_revision" not in {
            c["name"] for c in inspect(conn).get_columns("houses")
        }
    engine.dispose()


@pytest.mark.parametrize("mutation", [None, "name", "counter", "table"])
def test_source56_full_postmigration_delta_is_explicit(tmp_path, mutation):
    from tools.restore_policy import database_snapshot, verified_metadata_migration

    engine = create_engine(f"sqlite:///{tmp_path / 'delta.db'}")
    with engine.begin() as conn:
        cfg = Config()
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0016_durable_waits")
        conn.execute(
            text("INSERT INTO houses(id,name) VALUES('kept','Keep all original data')")
        )
        tables, rows, before = database_snapshot(conn)
        command.upgrade(cfg, "0017_metadata_cas")
        if mutation == "name":
            conn.execute(text("UPDATE houses SET name='unexpected' WHERE id='kept'"))
        elif mutation == "counter":
            conn.execute(text("UPDATE houses SET metadata_revision=2 WHERE id='kept'"))
        elif mutation == "table":
            conn.execute(text("CREATE TABLE unexpected_table(id INTEGER)"))
        if mutation:
            with pytest.raises(ValueError, match="Metadata migration changed"):
                verified_metadata_migration(conn, tables, rows)
        else:
            expected = verified_metadata_migration(conn, tables, rows)
            assert expected == database_snapshot(conn)[2]
            assert expected["scene_run_waits"] == before["scene_run_waits"]
            assert expected["device_commands"] == before["device_commands"]
            assert (
                conn.scalar(
                    text("SELECT metadata_revision FROM houses WHERE id='kept'")
                )
                == 1
            )
    engine.dispose()
