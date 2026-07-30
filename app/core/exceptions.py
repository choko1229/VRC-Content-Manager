from __future__ import annotations


class AppError(Exception):
    """Base class for application errors that should map to a clean HTTP response."""


class NotFoundError(AppError):
    def __init__(self, entity: str, entity_id: object) -> None:
        super().__init__(f"{entity} not found: {entity_id}")
        self.entity = entity
        self.entity_id = entity_id


class ValidationError(AppError):
    pass


class ConflictError(AppError):
    pass


class DriveError(AppError):
    """Raised when a Google Drive API call fails. Wraps the underlying cause."""
