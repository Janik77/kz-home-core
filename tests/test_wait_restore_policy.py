"""Offline NEW SQLite copy; original DB stays byte/logically unchanged."""

import asyncio
from pathlib import Path
import sqlite3
import pytest
from sqlalchemy import create_engine, text, event
from app.services.scene_run_service import SceneRunService
from sqlalchemy.orm import Session
from test_command_lifecycle import fixture
from test_durable_wait import actions, confirmed_first
from test_server_scene_execution import start, wait_action
from tools.restore_policy import full_fingerprints, invalidate_restored_waits


@pytest.mark.parametrize(
    "mode", ["valid", "claimed", "unexpected_delta", "fingerprint"]
)
def test_new_copy_policy_exact_delta_and_preserves_unknown(tmp_path, monkeypatch, mode):
    async def prepare():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _, _ = await start(h, actions(3600))
            run = response.json()
            await confirmed_first(h, run)
            unknown, _, _ = await start(h, [actions()[0]])
            unknown = unknown.json()
            await wait_action(h, unknown, 0)
            database = Path(h.engine.url.database)
        return database, run, unknown

    path, run, unknown = asyncio.run(prepare())
    source = create_engine("sqlite:///" + str(path))
    target_path = tmp_path / "new-copy.db"
    with sqlite3.connect(path) as old, sqlite3.connect(target_path) as new:
        old.backup(new)
    target = create_engine("sqlite:///" + str(target_path))
    with source.connect() as conn:
        before = full_fingerprints(conn)
    with target.connect() as conn:
        assert full_fingerprints(conn) == before
    if mode == "claimed":
        with target.begin() as conn:
            conn.execute(
                text(
                    "UPDATE scene_run_actions SET claimed=true WHERE run_id=:id AND position=2"
                ),
                {"id": run["id"]},
            )
    if mode == "unexpected_delta":

        def unwanted(conn, cursor, statement, params, context, executemany):
            if statement.startswith("UPDATE scene_runs"):
                conn.execute(text("UPDATE houses SET name='UNEXPECTED'"))

        event.listen(target, "after_cursor_execute", unwanted)
    expected = dict(before)
    if mode == "fingerprint":
        expected["scene_runs"] = {"rows": 2, "sha256": "a" * 64}
    if mode != "valid":
        with pytest.raises(
            ValueError,
            match="Malformed parked|outside exact allowed|differs from complete",
        ):
            with target.begin() as conn:
                invalidate_restored_waits(
                    conn, expected_before=expected if mode == "fingerprint" else None
                )
        with target.connect() as conn:
            assert (
                conn.scalar(
                    text("SELECT cancelled_at FROM scene_runs WHERE id=:id"),
                    {"id": run["id"]},
                )
                is None
            )
    else:
        with target.begin() as conn:
            report = invalidate_restored_waits(conn)
        assert report["changes"] == [
            dict(
                run_id=run["id"],
                wait_position=1,
                reason="restore_invalidation",
                cancelled_at=report["changes"][0]["cancelled_at"],
            )
        ]
        assert report["before"] == before
        assert {k for k in before if before[k] != report["after"][k]} == {"scene_runs"}
        with Session(target) as db:
            view = SceneRunService(db).get("fixture_house", run["id"])
            assert (
                view.status == "cancelled"
                and view.execution_error == "restore_invalidation"
            )
            assert [a.status for a in view.actions] == [
                "applied",
                "cancelled",
                "cancelled",
            ]
            assert (
                SceneRunService(db).get("fixture_house", unknown["id"]).status
                == "unknown"
            )
        with target.begin() as conn:
            second = invalidate_restored_waits(conn)
            assert second["changes"] == [] and second["before"] == second["after"]
    with source.connect() as conn:
        assert full_fingerprints(conn) == before
    source.dispose()
    target.dispose()


@pytest.mark.parametrize(
    "value", [None, {}, [], {"scene_runs": {"rows": True, "sha256": "a" * 64}}]
)
def test_invalid_complete_fingerprint_manifest(value):
    from tools.restore_policy import validate_fingerprint_manifest

    with pytest.raises(ValueError, match="fingerprint manifest"):
        validate_fingerprint_manifest(value)
