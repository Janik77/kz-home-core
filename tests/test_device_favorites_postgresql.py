# ruff: noqa: F811
"""Real PostgreSQL HTTP concurrency, FK cleanup and delete/insert races."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic, sleep

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.core import Settings
from app.models import DeviceFavoriteORM, DeviceORM, HouseMembershipORM
from app.repositories import MembershipRepository, UserRepository
from app.repositories.device_favorite_repository import DeviceFavoriteRepository
from app.services.auth_service import UserService
from test_definition_postgresql import pg_definitions  # noqa: F401
from test_api import create_app


@pytest.fixture
def pg_favorites(pg_definitions):
    ids = []
    with Session(pg_definitions) as session:
        for i in range(2):
            user = UserService(UserRepository(session)).create(
                f"favorites{i}@example.test", "favorite-test-password"
            )
            ids.append(user.id)
            MembershipRepository(session).create(
                {
                    "id": f"member{i}",
                    "user_id": user.id,
                    "house_id": "a",
                    "role": "resident",
                }
            )
    schema = pg_definitions.get_execution_options()["schema_translate_map"][None]
    settings = Settings(
        database_url=pg_definitions.url.update_query_dict(
            {"options": f"-csearch_path={schema} -clock_timeout=8000"}
        ).render_as_string(hide_password=False),
        app_env="test",
    )
    return pg_definitions, ids, settings


def test_postgresql_parallel_http_put_delete_and_restart(pg_favorites):
    engine, ids, settings = pg_favorites
    base = "/v1/houses/a/me/device-favorites"
    with TestClient(create_app(settings, run_simulator=False)) as client:
        tokens = []
        for i in range(2):
            response = client.post(
                "/auth/login",
                json={
                    "email": f"favorites{i}@example.test",
                    "password": "favorite-test-password",
                },
            )
            assert response.status_code == 200
            tokens.append(
                {"Authorization": "Bearer " + response.json()["access_token"]}
            )
        barrier = Barrier(4)

        def put(_):
            barrier.wait(10)
            return client.put(base + "/fixture", headers=tokens[0]).status_code

        with ThreadPoolExecutor(4) as workers:
            assert list(workers.map(put, range(4))) == [204] * 4
        assert client.get(base, headers=tokens[1]).json() == {"device_ids": []}
        assert client.put(base + "/fixture", headers=tokens[1]).status_code == 204
        with Session(engine) as session:
            assert session.query(DeviceFavoriteORM).count() == 2
        barrier = Barrier(2)

        def mixed(method):
            barrier.wait(10)
            return client.request(
                method, base + "/fixture", headers=tokens[0]
            ).status_code

        with ThreadPoolExecutor(2) as workers:
            assert list(workers.map(mixed, ["PUT", "DELETE"])) == [204, 204]
        assert client.delete(base + "/fixture", headers=tokens[0]).status_code == 204
        assert client.delete(base + "/fixture", headers=tokens[0]).status_code == 204
    with TestClient(create_app(settings, run_simulator=False)) as restarted:
        assert restarted.get(base, headers=tokens[0]).json() == {"device_ids": []}
        assert restarted.get(base, headers=tokens[1]).json() == {
            "device_ids": ["fixture"]
        }


def test_postgresql_bulk_membership_and_device_cascades(pg_favorites):
    engine, ids, _ = pg_favorites
    with Session(engine) as session:
        repo = DeviceFavoriteRepository(session)
        for user in ids:
            repo.put(user, "a", "fixture")
        session.execute(
            delete(HouseMembershipORM).where(HouseMembershipORM.user_id == ids[0])
        )
        session.commit()
        assert session.query(DeviceFavoriteORM).count() == 1
        MembershipRepository(session).create(
            {"id": "rejoined", "user_id": ids[0], "house_id": "a", "role": "resident"}
        )
        assert repo.list(ids[0], "a") == []
        session.execute(delete(DeviceORM).where(DeviceORM.id == "fixture"))
        session.commit()
        assert session.query(DeviceFavoriteORM).count() == 0


def test_postgresql_membership_revocation_serializes_with_uncommitted_put(pg_favorites):
    engine, ids, _ = pg_favorites
    inserted, release, deleting = Event(), Event(), Event()
    pid = []

    def put():
        with Session(engine) as session:

            def pause_before_commit(_):
                inserted.set()
                assert release.wait(10)

            event.listen(session, "before_commit", pause_before_commit)
            DeviceFavoriteRepository(session).put(ids[0], "a", "fixture")

    def revoke():
        with Session(engine) as session:
            pid.append(session.scalar(text("SELECT pg_backend_pid()")))
            deleting.set()
            session.execute(
                delete(HouseMembershipORM).where(HouseMembershipORM.user_id == ids[0])
            )
            session.commit()

    with ThreadPoolExecutor(2) as workers:
        writer = workers.submit(put)
        try:
            assert inserted.wait(10)
            revoker = workers.submit(revoke)
            assert deleting.wait(10)
            deadline = monotonic() + 10
            while True:
                with Session(engine) as session:
                    blocked = session.scalar(
                        text(
                            "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        {"pid": pid[0]},
                    )
                if blocked:
                    break
                assert monotonic() < deadline
                sleep(0.01)
        finally:
            release.set()
        writer.result(timeout=10)
        revoker.result(timeout=10)
    with Session(engine) as session:
        assert (
            session.scalar(
                select(DeviceFavoriteORM).where(DeviceFavoriteORM.user_id == ids[0])
            )
            is None
        )
        assert DeviceFavoriteRepository(session).list(ids[0], "a") == []
