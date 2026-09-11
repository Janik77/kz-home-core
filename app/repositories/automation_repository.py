from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import AutomationORM
from app.repositories.base import Repository


class AutomationRepository(Repository[AutomationORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AutomationORM)

    def enabled_for_house(self, house_id: str) -> list[AutomationORM]:
        statement = select(AutomationORM).where(
            AutomationORM.house_id == house_id,
            AutomationORM.enabled.is_(True),
        )
        return list(self.session.scalars(statement).all())
