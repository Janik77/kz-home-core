from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import Settings
from app.core.errors import ConflictError
from app.db import Base
from app.main import create_app
from app.models import HouseORM
from app.repositories import MembershipRepository, UserRepository
from app.security import Permission, PasswordManager, ROLE_PERMISSIONS, TokenCodec
from app.services.auth_service import AuthorizationService, UserService


def test_password_hashing_and_limits() -> None:
    manager = PasswordManager()
    encoded = manager.hash("correct horse battery staple")
    assert "correct horse" not in encoded
    assert manager.verify(encoded, "correct horse battery staple")
    assert not manager.verify(encoded, "wrong password")
    with pytest.raises(ValueError):
        manager.hash("short")


def _auth_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'auth.db'}",
        app_env="test",
        auth_jwt_secret="test-secret-that-is-at-least-32-bytes",
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserService(UserRepository(session)).create(
            " User@Example.COM ", "correct horse battery staple"
        )
        user_id = user.id
    return settings, engine, user_id


def test_login_refresh_rotation_logout_and_me(tmp_path) -> None:
    settings, engine, _ = _auth_client(tmp_path)
    try:
        with TestClient(create_app(settings, run_simulator=False)) as client:
            wrong = client.post(
                "/auth/login", json={"email": "user@example.com", "password": "wrong"}
            )
            unknown = client.post(
                "/auth/login", json={"email": "none@example.com", "password": "wrong"}
            )
            assert wrong.status_code == unknown.status_code == 401
            assert wrong.json() == unknown.json()
            login = client.post(
                "/auth/login",
                json={
                    "email": "USER@example.com",
                    "password": "correct horse battery staple",
                },
            )
            assert login.status_code == 200
            tokens = login.json()
            me = client.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            assert me.status_code == 200
            assert me.json()["email"] == "user@example.com"
            assert "password" not in me.text
            assert (
                client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
                ).status_code
                == 401
            )
            rotated = client.post(
                "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            assert rotated.status_code == 200
            assert (
                client.post(
                    "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
                ).status_code
                == 401
            )
            new_refresh = rotated.json()["refresh_token"]
            assert (
                client.post(
                    "/auth/logout", json={"refresh_token": new_refresh}
                ).status_code
                == 204
            )
            assert (
                client.post(
                    "/auth/refresh", json={"refresh_token": new_refresh}
                ).status_code
                == 401
            )
            assert (
                client.get(
                    "/auth/me", headers={"Authorization": "not-bearer"}
                ).status_code
                == 401
            )
    finally:
        engine.dispose()


def test_inactive_expired_and_tampered_tokens(tmp_path) -> None:
    settings, engine, user_id = _auth_client(tmp_path)
    codec = TokenCodec(settings.auth_jwt_secret, "HS256", 15, 30)
    expired, _ = codec.issue(
        user_id, "access", now=datetime.now(UTC) - timedelta(hours=1)
    )
    valid, _ = codec.issue(user_id, "access")
    with TestClient(create_app(settings, run_simulator=False)) as client:
        assert (
            client.get(
                "/auth/me", headers={"Authorization": f"Bearer {expired}"}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/auth/me", headers={"Authorization": f"Bearer {valid}x"}
            ).status_code
            == 401
        )
        with Session(engine) as session:
            user = UserRepository(session).get(user_id)
            user.is_active = False
            session.commit()
        assert (
            client.get(
                "/auth/me", headers={"Authorization": f"Bearer {valid}"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/auth/login",
                json={
                    "email": "user@example.com",
                    "password": "correct horse battery staple",
                },
            ).status_code
            == 401
        )
    engine.dispose()


def test_duplicate_email_membership_permissions_and_house_isolation(tmp_path) -> None:
    settings, engine, user_id = _auth_client(tmp_path)
    with Session(engine) as session:
        users = UserService(UserRepository(session))
        with pytest.raises(ConflictError):
            users.create("USER@example.com", "another secure password")
        session.add_all([HouseORM(id="a", name="A"), HouseORM(id="b", name="B")])
        session.commit()
        memberships = MembershipRepository(session)
        membership = memberships.create(
            {"id": "m1", "user_id": user_id, "house_id": "a", "role": "resident"}
        )
        with pytest.raises(ConflictError):
            memberships.create(
                {"id": "m2", "user_id": user_id, "house_id": "a", "role": "owner"}
            )
        authorization = AuthorizationService(memberships)
        assert authorization.has_permission(membership, Permission.DEVICE_CONTROL)
        assert not authorization.has_permission(membership, Permission.DEVICE_MANAGE)
        assert authorization.get_house_membership(user_id, "b") is None
        with pytest.raises(PermissionError):
            authorization.require_house_permission(user_id, "b", Permission.HOUSE_READ)
    assert ROLE_PERMISSIONS["owner"] >= set(Permission)
    assert Permission.MEMBER_MANAGE not in ROLE_PERMISSIONS["installer"]
    engine.dispose()


def test_production_requires_strong_secret() -> None:
    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        Settings(database_url="sqlite://", app_env="production")
