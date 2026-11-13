# ruff: noqa: F811
"""Real PostgreSQL creation/claim races; transport is explicitly a software recorder."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from uuid import uuid4

from alembic import command
from sqlalchemy.orm import Session
from sqlalchemy import inspect
import pytest

from app.models import SceneORM, SceneRunORM, SceneRunReceiptORM
from app.repositories import UserRepository
from app.schemas.scene_runs import SceneRunCreate
from app.services.auth_service import UserService
from app.services.scene_run_service import SceneRunService
from test_definition_postgresql import pg_definitions  # noqa: F401
from test_schema import migration_config


def test_postgresql_same_key_creation_and_dispatch_have_one_durable_winner(
    pg_definitions,
):
    with Session(pg_definitions) as session:
        user = UserService(UserRepository(session)).create(
            "runner@example.test", "scene-test-password"
        )
        user_id = user.id
        session.get(SceneORM, "scene").actions = [
            {"device_id": "fixture", "state": {"on": True}}
        ]
        session.commit()
    barrier = Barrier(2)
    key = str(uuid4())
    data = SceneRunCreate(scene_id="scene", expected_revision=1)

    def create(_):
        with Session(pg_definitions, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            return SceneRunService(session).create("a", user_id, key, data).id

    with ThreadPoolExecutor(2) as workers:
        ids = list(workers.map(create, range(2)))
    assert ids[0] == ids[1]
    run_id = ids[0]
    calls = []
    lock = Lock()

    class Wire:
        connected = True

        async def authorize_command(self, *args):
            pass

        async def send_command(self, *args, **kwargs):
            with lock:
                calls.append(kwargs["command_id"])
            # Verify the receipt and link are committed and visible BEFORE publish.
            with Session(pg_definitions) as observer:
                assert (
                    observer.get(SceneRunReceiptORM, (run_id, 0)).command_id == calls[0]
                )

    wire = Wire()
    barrier = Barrier(2)

    def dispatch(_):
        with Session(pg_definitions, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            return asyncio.run(
                SceneRunService(session, wire).dispatch("a", run_id, 0, user_id)
            )

    with ThreadPoolExecutor(2) as workers:
        list(workers.map(dispatch, range(2)))
    assert len(calls) == 1
    with Session(pg_definitions) as session:
        recovered = SceneRunService(session).get("a", run_id)
        assert recovered.actions[0].receipt.command_id == calls[0]
        assert recovered.status == "running"
        assert session.query(SceneRunORM).count() == 1
    # Nonempty history is protected against destructive schema rollback.
    with pg_definitions.begin() as connection:
        schema = pg_definitions.get_execution_options()["schema_translate_map"][None]
        from sqlalchemy import text

        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        with pytest.raises(RuntimeError, match="history is not empty"):
            command.downgrade(migration_config(connection), "0009_definition_revisions")
        assert "scene_runs" in inspect(connection).get_table_names(schema=schema)
