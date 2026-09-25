from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core import Settings


def clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DATABASE_URL",
        "APP_ENV",
        "APP_DEBUG",
        "KZHOME_SIMULATOR_ENABLED",
        "MQTT_ENABLED",
        "MQTT_HOST",
        "MQTT_PORT",
        "MQTT_USERNAME",
        "MQTT_PASSWORD",
        "MQTT_TLS_ENABLED",
        "MQTT_KEEPALIVE",
        "MQTT_CLIENT_ID",
        "AUTH_JWT_SECRET",
        "AUTH_JWT_ALGORITHM",
        "AUTH_ACCESS_TOKEN_MINUTES",
        "AUTH_REFRESH_TOKEN_DAYS",
        "AUTH_DEMO_EMAIL",
        "AUTH_DEMO_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_development_loads_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_settings_environment(monkeypatch)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=sqlite:///from-dotenv.db\nAPP_DEBUG=true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings.from_env()

    assert settings.database_url == "sqlite:///from-dotenv.db"
    assert settings.app_debug is True


def test_environment_variables_take_priority_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_settings_environment(monkeypatch)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=sqlite:///from-dotenv.db\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///from-environment.db")

    settings = Settings.from_env()

    assert settings.database_url == "sqlite:///from-environment.db"


def test_production_does_not_load_local_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_settings_environment(monkeypatch)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=sqlite:///must-not-be-loaded.db\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(
        RuntimeError, match="DATABASE_URL environment variable is required"
    ):
        Settings.from_env()


def test_mqtt_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_settings_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    settings = Settings.from_env()
    assert settings.mqtt_enabled is False
    assert settings.mqtt_port == 1883
    assert settings.mqtt_tls_enabled is False


def test_enabled_mqtt_requires_explicit_connection_identity() -> None:
    with pytest.raises(ValueError, match="MQTT_HOST and MQTT_CLIENT_ID"):
        Settings(database_url="sqlite:///:memory:", mqtt_enabled=True)


def test_production_mqtt_requires_tls() -> None:
    with pytest.raises(ValueError, match="TLS is required"):
        Settings(
            database_url="postgresql+psycopg://localhost/kzhome",
            app_env="production",
            mqtt_enabled=True,
            mqtt_host="broker.example",
            mqtt_client_id="core-1",
            auth_jwt_secret="test-only-jwt-secret-with-at-least-32-characters",
        )


def production_settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+psycopg://localhost/kzhome",
        "app_env": "production",
        "auth_jwt_secret": "test-only-jwt-secret-with-at-least-32-characters",
        "mqtt_enabled": True,
        "mqtt_host": "broker.example.invalid",
        "mqtt_client_id": "test-core",
        "mqtt_tls_enabled": True,
    }
    return Settings(**{**values, **overrides})


@pytest.mark.parametrize("value", ["prod", "prodution", "staging", "", "production "])
def test_unknown_environment_is_rejected(value, monkeypatch, tmp_path) -> None:
    clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", value)
    with pytest.raises(ValueError, match="APP_ENV"):
        Settings.from_env()
    with pytest.raises(ValueError, match="APP_ENV"):
        Settings(database_url="sqlite://", app_env=value)


def test_unknown_dotenv_environment_is_rejected(monkeypatch, tmp_path) -> None:
    clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("APP_ENV=prodution\n", encoding="utf-8")
    with pytest.raises(ValueError, match="APP_ENV"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["development", "test", "local", "TEST"])
def test_nonproduction_still_allows_sqlite_and_virtual_control(value) -> None:
    settings = Settings(database_url="sqlite://", app_env=value)
    assert settings.mqtt_enabled is False


@pytest.mark.parametrize(
    "url", ["sqlite://", "sqlite+pysqlite:///:memory:", "mysql://localhost/db", "bad-url"]
)
def test_production_rejects_non_postgresql(url) -> None:
    with pytest.raises(ValueError, match="DATABASE_URL must use PostgreSQL"):
        production_settings(database_url=url)


@pytest.mark.parametrize("url", ["postgresql://localhost/db", "postgresql+psycopg://localhost/db"])
def test_production_accepts_postgresql(url) -> None:
    assert production_settings(database_url=url, app_env="PRODUCTION").mqtt_enabled


def test_production_requires_mqtt_to_prevent_virtual_fallback() -> None:
    with pytest.raises(ValueError, match="requires MQTT_ENABLED=true"):
        production_settings(mqtt_enabled=False)


@pytest.mark.parametrize(
    "name", ["APP_DEBUG", "KZHOME_SIMULATOR_ENABLED", "MQTT_ENABLED", "MQTT_TLS_ENABLED"]
)
@pytest.mark.parametrize("value", ["", "tru", "2"])
def test_boolean_settings_reject_invalid_values(name, value, monkeypatch, tmp_path) -> None:
    clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"{name} must be a boolean"):
        Settings.from_env()


@pytest.mark.parametrize("value, expected", [("ON", True), ("off", False), ("1", True), ("0", False)])
def test_boolean_settings_parse_consistently(value, expected, monkeypatch, tmp_path) -> None:
    clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("MQTT_HOST", "broker.example.invalid")
    monkeypatch.setenv("MQTT_CLIENT_ID", "test-core")
    for name in ("APP_DEBUG", "KZHOME_SIMULATOR_ENABLED", "MQTT_ENABLED", "MQTT_TLS_ENABLED"):
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.app_debug is expected
    assert settings.simulator_enabled is expected
    assert settings.mqtt_enabled is expected
    assert settings.mqtt_tls_enabled is expected


def test_production_seed_refuses_before_database_access(monkeypatch) -> None:
    from app import seed

    engine_factory = Mock(side_effect=AssertionError("Database must not be opened"))
    monkeypatch.setattr(seed, "create_db_engine", engine_factory)
    with pytest.raises(RuntimeError, match="Demo seeding is forbidden"):
        seed.main(production_settings(app_env="PRODUCTION"))
    engine_factory.assert_not_called()
