from datetime import UTC, datetime
from uuid import uuid4

from app.core.errors import ConflictError, InvalidReferenceError
from app.models import HouseMembershipORM, UserORM
from app.repositories import (
    MembershipRepository,
    RefreshSessionRepository,
    UserRepository,
)
from app.security import (
    Permission,
    PasswordManager,
    ROLE_PERMISSIONS,
    TokenCodec,
    TokenError,
    hash_jti,
)


class AuthenticationError(ValueError):
    pass


class UserService:
    def __init__(self, users: UserRepository, passwords: PasswordManager | None = None):
        self.users = users
        self.passwords = passwords or PasswordManager()

    def create(self, email: str, password: str, **values: bool) -> UserORM:
        normalized = email.strip().lower()
        if self.users.by_email(normalized):
            raise ConflictError("Email is already registered")
        return self.users.create(
            {
                "id": str(uuid4()),
                "email": normalized,
                "password_hash": self.passwords.hash(password),
                **values,
            }
        )


class AuthorizationService:
    def __init__(self, memberships: MembershipRepository):
        self.memberships = memberships

    def get_house_membership(
        self, user_id: str, house_id: str
    ) -> HouseMembershipORM | None:
        return self.memberships.for_house(user_id, house_id)

    def accessible_house_ids(self, user_id: str, permission: Permission) -> list[str]:
        roles = {
            role
            for role, permissions in ROLE_PERMISSIONS.items()
            if permission in permissions
        }
        return self.memberships.house_ids(user_id, roles)

    @staticmethod
    def has_permission(
        membership: HouseMembershipORM | None, permission: Permission
    ) -> bool:
        return membership is not None and permission in ROLE_PERMISSIONS.get(
            membership.role, frozenset()
        )

    def require_house_permission(
        self, user_id: str, house_id: str, permission: Permission
    ) -> HouseMembershipORM:
        membership = self.get_house_membership(user_id, house_id)
        if not self.has_permission(membership, permission):
            raise PermissionError("House permission denied")
        assert membership is not None
        return membership


class MembershipService:
    def __init__(self, memberships: MembershipRepository, users: UserRepository):
        self.memberships = memberships
        self.users = users

    def list(self, house_id: str) -> list[HouseMembershipORM]:
        return self.memberships.for_house_members(house_id)

    def add(self, house_id: str, user_id: str, role: str) -> HouseMembershipORM:
        if not self.users.exists(user_id):
            raise InvalidReferenceError("Referenced user does not exist")
        return self.memberships.create(
            {"id": str(uuid4()), "house_id": house_id, "user_id": user_id, "role": role}
        )

    def change(self, house_id: str, user_id: str, role: str) -> HouseMembershipORM:
        membership = self.memberships.by_user_house(user_id, house_id)
        self._protect_final_owner(membership, role)
        return self.memberships.set_role(membership, role)

    def remove(self, house_id: str, user_id: str) -> None:
        membership = self.memberships.by_user_house(user_id, house_id)
        self._protect_final_owner(membership, None)
        self.memberships.delete(membership.id)

    def _protect_final_owner(
        self, membership: HouseMembershipORM, replacement_role: str | None
    ) -> None:
        owners = self.memberships.lock_owners(membership.house_id)
        if (
            membership.role == "owner"
            and replacement_role != "owner"
            and len(owners) <= 1
        ):
            raise ConflictError("The final house owner cannot be removed or downgraded")


class AuthenticationService:
    INVALID_CREDENTIALS = "Invalid email or password"

    def __init__(
        self,
        users: UserRepository,
        sessions: RefreshSessionRepository,
        codec: TokenCodec,
        passwords: PasswordManager | None = None,
    ):
        self.users = users
        self.sessions = sessions
        self.codec = codec
        self.passwords = passwords or PasswordManager()

    def login(self, email: str, password: str) -> dict:
        user = self.users.by_email(email)
        password_hash = (
            user.password_hash if user is not None else self.passwords.dummy_hash
        )
        password_valid = self.passwords.verify(password_hash, password)
        if user is None or not user.is_active or not password_valid:
            raise AuthenticationError(self.INVALID_CREDENTIALS)
        return self._issue_pair(user)

    def refresh(self, token: str) -> dict:
        try:
            claims = self.codec.decode(token, "refresh")
        except TokenError as error:
            raise AuthenticationError("Invalid refresh token") from error
        session = self.sessions.active(hash_jti(claims["jti"]))
        user = self.users.session.get(UserORM, claims["sub"])
        if (
            session is None
            or session.user_id != claims["sub"]
            or user is None
            or not user.is_active
        ):
            raise AuthenticationError("Invalid refresh token")
        self.sessions.revoke(session)
        return self._issue_pair(user)

    def logout(self, token: str) -> None:
        try:
            claims = self.codec.decode(token, "refresh")
        except TokenError as error:
            raise AuthenticationError("Invalid refresh token") from error
        session = self.sessions.active(hash_jti(claims["jti"]))
        if session is None:
            raise AuthenticationError("Invalid refresh token")
        self.sessions.revoke(session)

    def access_user(self, token: str) -> UserORM:
        try:
            claims = self.codec.decode(token, "access")
        except TokenError as error:
            raise AuthenticationError("Invalid access token") from error
        user = self.users.session.get(UserORM, claims["sub"])
        if user is None or not user.is_active:
            raise AuthenticationError("Invalid access token")
        return user

    def _issue_pair(self, user: UserORM) -> dict:
        access, _ = self.codec.issue(user.id, "access")
        refresh, claims = self.codec.issue(user.id, "refresh")
        expires_at = (
            datetime.fromtimestamp(claims["exp"], UTC)
            if isinstance(claims["exp"], int)
            else claims["exp"]
        )
        self.sessions.create(
            {
                "jti_hash": hash_jti(claims["jti"]),
                "user_id": user.id,
                "expires_at": expires_at,
            }
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": self.codec.access_minutes * 60,
        }
