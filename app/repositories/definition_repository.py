"""Atomic definition edits shared by scenes and automations."""

from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, DefinitionRevisionConflict
from app.repositories.base import ModelT, Repository


class DefinitionRepository(Repository[ModelT]):
    def update(
        self,
        entity_id: str,
        values: dict[str, Any],
        *,
        expected_revision: int | None = None,
        expected_house_id: str | None = None,
    ) -> ModelT:
        current = self.get(entity_id)
        revision = current.revision if expected_revision is None else expected_revision
        statement = update(self.model).where(
            self.model.id == entity_id,
            self.model.revision == revision,
        )
        if expected_house_id is not None:
            statement = statement.where(self.model.house_id == expected_house_id)
        # A no-op PATCH must still execute the compare-and-swap. Every successful
        # edit, including legacy PATCH and enable/disable, invalidates old drafts.
        statement = statement.values(
            **self._orm_values(values),
            revision=self.model.revision + 1,
        ).returning(self.model)
        try:
            result = self.session.scalars(
                statement,
                execution_options={
                    "populate_existing": True,
                    "synchronize_session": False,
                },
            ).one_or_none()
            if result is None:
                self.session.rollback()
                raise DefinitionRevisionConflict()
            self._commit()
            return result
        except IntegrityError as error:
            self.session.rollback()
            raise ConflictError("Definition conflicts with related data") from error
