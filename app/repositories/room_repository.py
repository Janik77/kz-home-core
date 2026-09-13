from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import FloorORM, RoomORM
from app.repositories.base import Repository


class RoomRepository(Repository[RoomORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, RoomORM)

    def house_id(self, room_id: str) -> str:
        value = self.session.scalar(
            select(FloorORM.house_id).join(RoomORM).where(RoomORM.id == room_id)
        )
        if value is None:
            self.get(room_id)
        return value  # type: ignore[return-value]

    def for_houses(self, house_ids: list[str]) -> list[RoomORM]:
        if not house_ids:
            return []
        statement = (
            select(RoomORM).join(FloorORM).where(FloorORM.house_id.in_(house_ids))
        )
        return list(self.session.scalars(statement).all())
