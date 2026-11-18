from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import AutomationORM
from app.repositories.definition_repository import DefinitionRepository


class AutomationRepository(DefinitionRepository[AutomationORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AutomationORM)

    def create(self, values):
        from app.services.command_clock import command_clock

        return super().create(
            {**values, "schedule_effective_after": command_clock.now()}
        )

    def update(self, entity_id, values, **kwargs):
        from app.services.command_clock import command_clock

        return super().update(
            entity_id,
            {**values, "schedule_effective_after": command_clock.now()},
            **kwargs,
        )

    def enabled_for_house(self, house_id: str) -> list[AutomationORM]:
        statement = select(AutomationORM).where(
            AutomationORM.house_id == house_id,
            AutomationORM.enabled.is_(True),
        )
        return list(self.session.scalars(statement).all())

    def for_houses(self, house_ids: list[str]) -> list[AutomationORM]:
        if not house_ids:
            return []
        return list(
            self.session.scalars(
                select(AutomationORM).where(AutomationORM.house_id.in_(house_ids))
            ).all()
        )
