from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_DUMMY_PASSWORD_HASH = PasswordHasher().hash("not-a-real-user-password")


class Permission(StrEnum):
    HOUSE_READ = "house.read"
    HOUSE_MANAGE = "house.manage"
    MEMBER_READ = "member.read"
    MEMBER_MANAGE = "member.manage"
    DEVICE_READ = "device.read"
    DEVICE_CONTROL = "device.control"
    DEVICE_MANAGE = "device.manage"
    SCENE_READ = "scene.read"
    SCENE_RUN = "scene.run"
    SCENE_MANAGE = "scene.manage"
    AUTOMATION_READ = "automation.read"
    AUTOMATION_MANAGE = "automation.manage"
    EVENT_READ = "event.read"
    SECURITY_AUDIT_READ = "security.audit.read"


ALL_PERMISSIONS = frozenset(Permission)
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "owner": ALL_PERMISSIONS,
    "installer": frozenset(
        {
            Permission.HOUSE_READ,
            Permission.DEVICE_READ,
            Permission.DEVICE_CONTROL,
            Permission.DEVICE_MANAGE,
            Permission.SCENE_READ,
            Permission.SCENE_RUN,
            Permission.SCENE_MANAGE,
            Permission.AUTOMATION_READ,
            Permission.AUTOMATION_MANAGE,
            Permission.EVENT_READ,
        }
    ),
    "technician": frozenset(
        {
            Permission.HOUSE_READ,
            Permission.DEVICE_READ,
            Permission.DEVICE_CONTROL,
            Permission.DEVICE_MANAGE,
            Permission.EVENT_READ,
        }
    ),
    "resident": frozenset(
        {
            Permission.HOUSE_READ,
            Permission.DEVICE_READ,
            Permission.DEVICE_CONTROL,
            Permission.SCENE_READ,
            Permission.SCENE_RUN,
        }
    ),
}


class PasswordManager:
    MIN_LENGTH = 12
    MAX_LENGTH = 256

    def __init__(self) -> None:
        self._hasher = PasswordHasher()

    def hash(self, password: str) -> str:
        if not self.MIN_LENGTH <= len(password) <= self.MAX_LENGTH:
            raise ValueError("Password must be between 12 and 256 characters")
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        if len(password) > self.MAX_LENGTH:
            return False
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    @property
    def dummy_hash(self) -> str:
        """A valid hash used to keep unknown-user verification work equivalent."""
        return _DUMMY_PASSWORD_HASH


class TokenError(ValueError):
    pass


class TokenCodec:
    def __init__(
        self, secret: str, algorithm: str, access_minutes: int, refresh_days: int
    ):
        self.secret = secret
        self.algorithm = algorithm
        self.access_minutes = access_minutes
        self.refresh_days = refresh_days

    def issue(
        self, subject: str, token_type: str, *, now: datetime | None = None
    ) -> tuple[str, dict]:
        issued = now or datetime.now(UTC)
        lifetime = (
            timedelta(minutes=self.access_minutes)
            if token_type == "access"
            else timedelta(days=self.refresh_days)
        )
        claims = {
            "sub": subject,
            "type": token_type,
            "iat": issued,
            "exp": issued + lifetime,
            "jti": str(uuid4()),
        }
        return jwt.encode(claims, self.secret, algorithm=self.algorithm), claims

    def decode(self, token: str, expected_type: str) -> dict:
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=[self.algorithm],
                options={"require": ["sub", "type", "iat", "exp", "jti"]},
            )
        except jwt.PyJWTError as error:
            raise TokenError("Invalid or expired token") from error
        if claims["type"] != expected_type:
            raise TokenError("Invalid token type")
        return claims


def hash_jti(jti: str) -> str:
    return sha256(jti.encode()).hexdigest()
