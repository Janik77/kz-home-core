from sqlalchemy.orm import Session
from app.models import FloorORM
from app.repositories.base import Repository


class FloorRepository(Repository[FloorORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, FloorORM)
