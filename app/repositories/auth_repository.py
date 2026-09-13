from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HouseMembershipORM, RefreshSessionORM, UserORM
from app.repositories.base import Repository


class UserRepository(Repository[UserORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, UserORM)

    def by_email(self, email: str) -> UserORM | None:
        return self.session.scalar(
            select(UserORM).where(UserORM.email == email.strip().lower())
        )


class MembershipRepository(Repository[HouseMembershipORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, HouseMembershipORM)

    def for_house(self, user_id: str, house_id: str) -> HouseMembershipORM | None:
        return self.session.scalar(
            select(HouseMembershipORM).where(
                HouseMembershipORM.user_id == user_id,
                HouseMembershipORM.house_id == house_id,
            )
        )


class RefreshSessionRepository(Repository[RefreshSessionORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, RefreshSessionORM)

    def active(self, jti_hash: str) -> RefreshSessionORM | None:
        item = self.session.get(RefreshSessionORM, jti_hash)
        now = datetime.now(UTC)
        if (
            item is None
            or item.revoked_at is not None
            or item.expires_at.replace(tzinfo=item.expires_at.tzinfo or UTC) <= now
        ):
            return None
        return item

    def revoke(self, item: RefreshSessionORM) -> None:
        item.revoked_at = datetime.now(UTC)
        self._commit()
