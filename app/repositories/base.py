from typing import Any, Generic, TypeVar

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError, EntityNotFoundError, MetadataRevisionConflict, InvalidReferenceError,
)
from app.db import Base

ModelT = TypeVar("ModelT", bound=Base)


class Repository(Generic[ModelT]):
    def __init__(self, session: Session, model: type[ModelT]) -> None:
        self.session = session
        self.model = model

    def list(self) -> list[ModelT]:
        return list(self.session.scalars(select(self.model)).all())

    def get(self, entity_id: str) -> ModelT:
        entity = self.session.get(self.model, entity_id)
        if entity is None:
            raise EntityNotFoundError(f"{self.model.__name__} not found")
        return entity

    def create(self, values: dict[str, Any]) -> ModelT:
        entity = self.model(**self._orm_values(values))
        self.session.add(entity)
        self._commit()
        return entity

    def update(self, entity_id: str, values: dict[str, Any]) -> ModelT:
        values = dict(values)
        self.validate_metadata_token(values)
        expected = values.pop("expected_metadata_revision", None)
        if expected is not None:
            row = self.session.scalar(
                update(self.model)
                .where(self.model.id == entity_id,
                       self.model.metadata_revision == expected)
                .values(**self._orm_values(values),
                        metadata_revision=self.model.metadata_revision + 1)
                .returning(self.model),
                execution_options={"populate_existing": True},
            )
            if row is None:
                self.session.rollback()
                raise MetadataRevisionConflict()
            self._commit()
            return row
        entity = self.get(entity_id)
        for key, value in self._orm_values(values).items():
            setattr(entity, key, value)
        self._commit()
        return entity

    def validate_metadata_token(self, values: dict[str, Any]) -> None:
        if not hasattr(self.model, "metadata_revision"):
            return
        expected = values.get("expected_metadata_revision")
        protected = {
            "name", "house_id", "floor_id", "room_id", "order", "icon",
            "type", "capabilities", "metadata", "metadata_",
        }
        if expected is None and not (protected & values.keys()):
            return
        if type(expected) is not int or not 1 <= expected <= 2147483646:
            raise InvalidReferenceError("Metadata edit requires a valid expected_metadata_revision")

    def delete(self, entity_id: str) -> None:
        self.session.delete(self.get(entity_id))
        self._commit()

    def exists(self, entity_id: str) -> bool:
        return self.session.get(self.model, entity_id) is not None

    def _commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise ConflictError(
                "Entity already exists or conflicts with related data"
            ) from error

    @staticmethod
    def _orm_values(values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        if "metadata" in values:
            values["metadata_"] = values.pop("metadata")
        for field in ("actions", "trigger", "conditions"):
            value = values.get(field)
            if isinstance(value, list):
                values[field] = [
                    item.model_dump() if hasattr(item, "model_dump") else item
                    for item in value
                ]
            elif value is not None and hasattr(value, "model_dump"):
                values[field] = value.model_dump()
        return values
