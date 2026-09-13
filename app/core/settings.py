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
    mqtt_enabled: bool = False
    mqtt_host: str | None = None
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_tls_enabled: bool = False
    mqtt_keepalive: int = 60
    mqtt_client_id: str | None = None
    auth_jwt_secret: str = "development-only-change-me-32-bytes"
    auth_jwt_algorithm: str = "HS256"
    auth_access_token_minutes: int = 15
    auth_refresh_token_days: int = 30

    def __post_init__(self) -> None:
        if self.auth_jwt_algorithm not in {"HS256", "HS384", "HS512"}:
            raise ValueError("AUTH_JWT_ALGORITHM must be an approved HMAC algorithm")
        if self.auth_access_token_minutes < 1 or self.auth_refresh_token_days < 1:
            raise ValueError("Authentication token lifetimes must be positive")
        if self.app_env.lower() == "production" and (
            len(self.auth_jwt_secret) < 32
            or self.auth_jwt_secret == "development-only-change-me-32-bytes"
            or "change-me" in self.auth_jwt_secret.lower()
        ):
            raise ValueError("Production requires a strong AUTH_JWT_SECRET")
        if not self.mqtt_enabled:
            return
        if not self.mqtt_host or not self.mqtt_client_id:
            raise ValueError(
                "MQTT_HOST and MQTT_CLIENT_ID are required when MQTT is enabled"
            )
        if not 1 <= self.mqtt_port <= 65535:
            raise ValueError("MQTT_PORT must be between 1 and 65535")
        if not 1 <= self.mqtt_keepalive <= 65535:
            raise ValueError("MQTT_KEEPALIVE must be between 1 and 65535")
        if (self.mqtt_username is None) != (self.mqtt_password is None):
            raise ValueError("MQTT_USERNAME and MQTT_PASSWORD must be set together")
        if self.app_env.lower() == "production" and not self.mqtt_tls_enabled:
            raise ValueError("MQTT TLS is required in production")

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
            mqtt_enabled=_boolean("MQTT_ENABLED", False),
            mqtt_host=_optional("MQTT_HOST"),
            mqtt_port=_integer("MQTT_PORT", 1883),
            mqtt_username=_optional("MQTT_USERNAME"),
            mqtt_password=_optional("MQTT_PASSWORD"),
            mqtt_tls_enabled=_boolean("MQTT_TLS_ENABLED", False),
            mqtt_keepalive=_integer("MQTT_KEEPALIVE", 60),
            mqtt_client_id=_optional("MQTT_CLIENT_ID"),
            auth_jwt_secret=os.getenv(
                "AUTH_JWT_SECRET", "development-only-change-me-32-bytes"
            ),
            auth_jwt_algorithm=os.getenv("AUTH_JWT_ALGORITHM", "HS256"),
            auth_access_token_minutes=_integer("AUTH_ACCESS_TOKEN_MINUTES", 15),
            auth_refresh_token_days=_integer("AUTH_REFRESH_TOKEN_DAYS", 30),
        )


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError(f"{name} must be a boolean")
    return normalized in {"1", "true", "yes", "on"}


def _integer(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _optional(name: str) -> str | None:
    return os.getenv(name) or None
