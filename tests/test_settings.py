from pathlib import Path

import pytest

from app.core import Settings


def clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "APP_ENV", "APP_DEBUG", "KZHOME_SIMULATOR_ENABLED"):
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
