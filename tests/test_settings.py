from pathlib import Path

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
            database_url="sqlite:///:memory:",
            app_env="production",
            mqtt_enabled=True,
            mqtt_host="broker.example",
            mqtt_client_id="core-1",
        )
