from sqlalchemy.orm import Session
from app.models import RoomORM
from app.repositories.base import Repository


class RoomRepository(Repository[RoomORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, RoomORM)
