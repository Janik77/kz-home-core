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

    def house_ids(self, user_id: str, permission_roles: set[str]) -> list[str]:
        statement = select(HouseMembershipORM.house_id).where(
            HouseMembershipORM.user_id == user_id,
            HouseMembershipORM.role.in_(permission_roles),
        )
        return list(self.session.scalars(statement).all())

    def for_user_houses(self, user_id: str) -> list[HouseMembershipORM]:
        return list(
            self.session.scalars(
                select(HouseMembershipORM).where(HouseMembershipORM.user_id == user_id)
            ).all()
        )

    def for_house_members(self, house_id: str) -> list[HouseMembershipORM]:
        return list(
            self.session.scalars(
                select(HouseMembershipORM).where(
                    HouseMembershipORM.house_id == house_id
                )
            ).all()
        )

    def by_user_house(self, user_id: str, house_id: str) -> HouseMembershipORM:
        item = self.for_house(user_id, house_id)
        if item is None:
            from app.core.errors import EntityNotFoundError

            raise EntityNotFoundError("House membership not found")
        return item

    def lock_owners(self, house_id: str) -> list[HouseMembershipORM]:
        """Serialize final-owner mutations on databases that support row locks."""
        statement = (
            select(HouseMembershipORM)
            .where(
                HouseMembershipORM.house_id == house_id,
                HouseMembershipORM.role == "owner",
            )
            .with_for_update()
        )
        return list(self.session.scalars(statement).all())

    def set_role(self, membership: HouseMembershipORM, role: str) -> HouseMembershipORM:
        membership.role = role
        self._commit()
        return membership


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
