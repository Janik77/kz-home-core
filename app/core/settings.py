import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str = "development"
    app_debug: bool = False
    simulator_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        # Local files are a development convenience only. Explicit process
        # environment values win because python-dotenv never overrides them.
        app_env = os.getenv("APP_ENV", "development")
        if app_env.lower() in {"development", "test", "local"}:
            load_dotenv(Path.cwd() / ".env", override=False)

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        return cls(
            database_url=database_url,
            app_env=os.getenv("APP_ENV", "development"),
            app_debug=os.getenv("APP_DEBUG", "false").lower() in {"1", "true", "yes"},
            simulator_enabled=os.getenv("KZHOME_SIMULATOR_ENABLED", "false").lower()
            in {"1", "true", "yes"},
        )
