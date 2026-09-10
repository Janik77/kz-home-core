from sqlalchemy.orm import Session
from app.models import AutomationORM
from app.repositories.base import Repository


class AutomationRepository(Repository[AutomationORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AutomationORM)
