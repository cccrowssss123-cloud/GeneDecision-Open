"""Exception types shared across GeneDecision.

These live in their own module so that ingestion, normalization, and storage can
raise (and catch) the same errors without importing one another.
"""

from __future__ import annotations

from typing import Any


class GeneDecisionError(Exception):
    """Base class for every GeneDecision-specific error."""


class EvidenceImportError(GeneDecisionError, ValueError):
    """Raised when an evidence source cannot be turned into ``EvidenceRecord``s.

    Whenever the importer can determine them, the error carries the source, the
    row number inside that source, the offending field, and the raw value that
    was supplied, so the user can go straight to the offending cell instead of
    guessing. Nothing is repaired on the user's behalf.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        row_number: int | None = None,
        field: str | None = None,
        value: Any = None,
    ) -> None:
        self.message = message
        self.source = source
        self.row_number = row_number
        self.field = field
        self.value = value
        super().__init__(self._describe())

    def _describe(self) -> str:
        parts = [self.message]
        location = []
        if self.source is not None:
            location.append(f"source={self.source}")
        if self.row_number is not None:
            location.append(f"row={self.row_number}")
        if self.field is not None:
            location.append(f"field={self.field}")
        if location:
            parts.append("(" + ", ".join(location) + ")")
        if self.value is not None:
            parts.append(f"[supplied value: {self.value!r}]")
        return " ".join(parts)


class UnsupportedEvidenceFormatError(EvidenceImportError):
    """Raised when a file extension has no registered reader."""


class DuplicateEvidenceError(EvidenceImportError):
    """Raised when the same ``evidence_id`` is supplied more than once."""


class EvidenceStorageError(GeneDecisionError):
    """Raised when persistence fails for a reason other than a duplicate key."""

    def __init__(self, message: str, *, source: str | None = None) -> None:
        self.message = message
        self.source = source
        super().__init__(
            f"{message} (source={source})" if source is not None else message
        )
