import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str = "development"
    app_debug: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        return cls(
            database_url=database_url,
            app_env=os.getenv("APP_ENV", "development"),
            app_debug=os.getenv("APP_DEBUG", "false").lower() in {"1", "true", "yes"},
        )
