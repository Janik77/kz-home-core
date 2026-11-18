from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models import HouseMembershipORM, HouseORM
from app.repositories.base import Repository


class HouseRepository(Repository[HouseORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, HouseORM)

    def update(self, entity_id, values):
        values = dict(values)
        self.validate_metadata_token(values)
        expected = values.pop("expected_timezone_revision", None)
        if "timezone" not in values:
            return super().update(entity_id, values)
        from app.services.command_clock import command_clock
        from app.core.errors import DefinitionRevisionConflict

        metadata_expected = values.pop("expected_metadata_revision", None)
        predicates = [HouseORM.id == entity_id, HouseORM.timezone_revision == expected]
        if metadata_expected is not None:
            predicates.append(HouseORM.metadata_revision == metadata_expected)
            values["metadata_revision"] = HouseORM.metadata_revision + 1
        # Atomic CAS includes a no-op save: all pending old-zone intents expire.
        values.update(
            timezone_revision=HouseORM.timezone_revision + 1,
            timezone_effective_after=command_clock.now(),
        )
        row = self.session.scalar(
            update(HouseORM)
            .where(*predicates)
            .values(**values)
            .returning(HouseORM),
            execution_options={"populate_existing": True},
        )
        if row is None:
            self.session.rollback()
            if metadata_expected is not None:
                from app.core.errors import MetadataRevisionConflict
                raise MetadataRevisionConflict()
            raise DefinitionRevisionConflict()
        self._commit()
        return row

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
