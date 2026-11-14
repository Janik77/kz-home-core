"""Real PostgreSQL CAS races; only an explicit dedicated TEST_POSTGRESQL_URL."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.errors import DefinitionRevisionConflict
from app.models import AutomationORM, DeviceORM, FloorORM, HouseORM, RoomORM, SceneORM
from app.repositories.automation_repository import AutomationRepository
from app.repositories.scene_repository import SceneRepository


@pytest.fixture
def pg_definitions():
    url = os.getenv("TEST_POSTGRESQL_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRESQL_URL to a dedicated PostgreSQL test database")
    if make_url(url).get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRESQL_URL must use PostgreSQL")
    engine = create_engine(url)
    schema = "kz_definition_test_" + uuid4().hex
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config()
            config.set_main_option(
                "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
            )
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        with Session(scoped) as session:
            session.add_all([HouseORM(id="a", name="A"), HouseORM(id="b", name="B")])
            session.flush()
            session.add(FloorORM(id="floor", house_id="a", name="Floor", order=0))
            session.flush()
            session.add(RoomORM(id="room", floor_id="floor", name="Room"))
            session.flush()
            session.add(
                DeviceORM(
                    id="fixture",
                    room_id="room",
                    name="Software fixture",
                    type="relay",
                    online=True,
                    capabilities=["on_off"],
                    state={"on": False},
                    metadata_={},
                )
            )
            session.flush()
            session.add_all(
                [
                    SceneORM(id="scene", name="Original", house_id="a", actions=[]),
                    AutomationORM(
                        id="rule",
                        name="Original",
                        house_id="a",
                        enabled=False,
                        trigger={"device_id": "fixture", "field": "on", "value": True},
                        conditions=[],
                        actions=[{"type": "delay", "seconds": 1}],
                    ),
                ]
            )
            session.commit()
        yield scoped
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


CASES = [
    (SceneRepository, SceneORM, "scene"),
    (AutomationRepository, AutomationORM, "rule"),
]


@pytest.mark.parametrize("repository,model,identifier", CASES)
@pytest.mark.parametrize("legacy_writer", [False, True])
def test_postgresql_two_editors_have_one_atomic_winner(
    pg_definitions, repository, model, identifier, legacy_writer
):
    barrier = Barrier(2)

    def edit(index):
        with Session(pg_definitions, expire_on_commit=False) as session:
            session.execute(text("SET lock_timeout = '5s'"))
            repo = repository(session)
            snapshot = repo.get(identifier)  # Keep identity-map snapshot alive.
            assert snapshot.revision == 1
            barrier.wait(timeout=5)
            kwargs = {} if legacy_writer and index == 1 else {"expected_revision": 1}
            try:
                result = repo.update(
                    identifier,
                    {"name": f"Editor {index}"},
                    expected_house_id="a",
                    **kwargs,
                )
                return "saved", result.name, result.revision
            except DefinitionRevisionConflict:
                # The rejected SQL transaction is rolled back and usable again.
                return "conflict", repo.get(identifier).name, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, [0, 1]))
    assert sorted(item[0] for item in results) == ["conflict", "saved"]
    winner = next(item for item in results if item[0] == "saved")
    assert winner[2] == 2
    with Session(pg_definitions) as session:
        current = session.get(model, identifier)
        assert current.revision == 2 and current.name == winner[1]
        assert current.actions == (
            [] if identifier == "scene" else [{"type": "delay", "seconds": 1}]
        )


@pytest.mark.parametrize("repository,model,identifier", CASES)
def test_postgresql_legacy_write_invalidates_old_revision(
    pg_definitions, repository, model, identifier
):
    with Session(pg_definitions) as first, Session(pg_definitions) as second:
        stale = repository(first).get(identifier)
        repository(second).update(identifier, {"name": "Legacy"})
        with pytest.raises(DefinitionRevisionConflict):
            repository(first).update(
                identifier, {"name": "Old draft"}, expected_revision=stale.revision
            )
    with Session(pg_definitions) as session:
        assert session.get(model, identifier).name == "Legacy"
        assert session.get(model, identifier).revision == 2


@pytest.mark.parametrize("repository,model,identifier", CASES)
def test_postgresql_source_house_is_part_of_atomic_edit(
    pg_definitions, repository, model, identifier
):
    with Session(pg_definitions) as session:
        repo = repository(session)
        moved = repo.update(
            identifier, {"house_id": "b"}, expected_revision=1, expected_house_id="a"
        )
        assert moved.revision == 2
        with pytest.raises(DefinitionRevisionConflict):
            repo.update(
                identifier,
                {"name": "Wrong authorization"},
                expected_revision=2,
                expected_house_id="a",
            )
        assert repo.get(identifier).name == "Original"
        assert repo.get(identifier).house_id == "b"


def test_postgresql_enable_invalidates_rename(pg_definitions):
    with Session(pg_definitions) as session:
        repo = AutomationRepository(session)
        assert repo.update("rule", {"enabled": True}).revision == 2
        with pytest.raises(DefinitionRevisionConflict):
            repo.update("rule", {"name": "Old draft"}, expected_revision=1)
        assert repo.get("rule").enabled and repo.get("rule").name == "Original"


@pytest.mark.parametrize(
    "kind,identifier", [("scene", "scene"), ("automation", "rule")]
)
@pytest.mark.parametrize("clock_fault", [None, "future_iat", "expired"])
def test_postgresql_parallel_http_edits_conflict_after_both_validated(
    pg_definitions, monkeypatch, kind, identifier, clock_fault
):
    # Use actual auth, service validation and request-owned DB sessions. The
    # barrier deliberately makes both requests validate the same revision.
    from fastapi.testclient import TestClient
    from app.core import Settings
    from app.main import create_app
    from app.repositories import MembershipRepository, UserRepository
    from app.services.auth_service import UserService
    from app.services.scene_service import SceneService
    from app.services.automation_service import AutomationService

    schema = pg_definitions.get_execution_options()["schema_translate_map"][None]
    url = pg_definitions.url.update_query_dict(
        {"options": f"-csearch_path={schema} -clock_timeout=5000"}
    )
    settings = Settings(
        database_url=url.render_as_string(hide_password=False), app_env="test"
    )
    with Session(pg_definitions) as session:
        user = UserService(UserRepository(session)).create(
            "editor@example.test", "test-definition-password"
        )
        MembershipRepository(session).create(
            {
                "id": "editor-membership",
                "user_id": user.id,
                "house_id": "a",
                "role": "owner",
            }
        )
    from app.security import TokenCodec, TokenError

    token_failures = []
    decode = TokenCodec.decode

    def diagnosed_decode(self, token, expected_type):
        try:
            return decode(self, token, expected_type)
        except TokenError as error:
            # Never include the token, claims, user identifier or exception text.
            token_failures.append(type(error.__cause__).__name__)
            raise

    monkeypatch.setattr(TokenCodec, "decode", diagnosed_decode)
    barrier = Barrier(2)
    service = SceneService if kind == "scene" else AutomationService
    validate = service._validate

    def synchronized_validation(self, candidate):
        validate(self, candidate)
        barrier.wait(timeout=10)

    monkeypatch.setattr(service, "_validate", synchronized_validation)
    with TestClient(create_app(settings, run_simulator=False)) as client:
        login = client.post(
            "/auth/login",
            json={
                "email": "editor@example.test",
                "password": "test-definition-password",
            },
        )
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        path = f"/{kind}s/{identifier}"
        initial = client.get(path, headers=headers)
        assert initial.status_code == 200, initial.text
        original = initial.json()
        assert original["revision"] == 1

        if clock_fault is not None:
            from datetime import UTC, datetime
            import jwt.api_jwt

            claims = TokenCodec(settings.auth_jwt_secret, "HS256", 15, 30).decode(
                login.json()["access_token"], "access"
            )
            timestamp = (
                claims["iat"] - 2 if clock_fault == "future_iat" else claims["exp"]
            )

            class RejectionClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls.fromtimestamp(timestamp, UTC)

            monkeypatch.setattr(jwt.api_jwt, "datetime", RejectionClock)

        def edit(index):
            return client.patch(
                path,
                headers=headers,
                json={"name": f"HTTP editor {index}", "expected_revision": 1},
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(edit, [0, 1]))
        if clock_fault is not None:
            assert [response.status_code for response in responses] == [401, 401]
            assert all(
                response.json() == {"detail": "Invalid access token"}
                for response in responses
            )
            reason = (
                "ImmatureSignatureError"
                if clock_fault == "future_iat"
                else "ExpiredSignatureError"
            )
            assert token_failures == [reason, reason]
            model = SceneORM if kind == "scene" else AutomationORM
            with Session(pg_definitions) as session:
                persisted = session.get(model, identifier)
                assert persisted.revision == 1 and persisted.name == "Original"
            return
        assert sorted(response.status_code for response in responses) == [200, 409], {
            "responses": [
                (response.status_code, response.json()) for response in responses
            ],
            "jwt_error_classes": token_failures,
        }
        rejected = next(
            response for response in responses if response.status_code == 409
        )
        assert rejected.json()["code"] == "definition_revision_conflict"
        winner = next(
            response.json() for response in responses if response.status_code == 200
        )
        persisted = client.get(path, headers=headers).json()
        assert persisted == winner and persisted["revision"] == 2
        for field in original.keys() - {"revision", "name", "updated_at"}:
            assert persisted[field] == original[field]
