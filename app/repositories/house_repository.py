from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models import HouseMembershipORM, HouseORM
from app.repositories.base import Repository


class HouseRepository(Repository[HouseORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, HouseORM)

    def for_ids(self, house_ids: list[str]) -> list[HouseORM]:
        if not house_ids:
            return []
        return list(
            self.session.scalars(
                select(HouseORM).where(HouseORM.id.in_(house_ids))
            ).all()
        )

    def create_with_owner(self, values: dict, user_id: str) -> HouseORM:
        house = HouseORM(**values)
        membership = HouseMembershipORM(
            id=str(uuid4()), user_id=user_id, house_id=house.id, role="owner"
        )
        try:
            self.session.add(house)
            self.session.flush()
            self.session.add(membership)
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise ConflictError("House already exists") from error
        return house

    def delete_with_memberships(self, house_id: str) -> None:
        house = self.get(house_id)
        try:
            self.session.execute(
                delete(HouseMembershipORM).where(
                    HouseMembershipORM.house_id == house_id
                )
            )
            self.session.delete(house)
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise ConflictError("House cannot be deleted") from error
