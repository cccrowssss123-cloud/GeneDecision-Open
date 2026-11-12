"""Structural dataset contracts, independent of scientific evidence judgments."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_SCHEMA_VERSION = "dataset-manifest-v1"
Count = Annotated[int, Field(strict=True, ge=0)]
Name = Annotated[str, Field(strict=True, min_length=1)]


class StructuralModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetType(StrEnum):
    UNKNOWN = "UNKNOWN"
    CRISPR = "CRISPR"
    RNA_SEQ = "RNA_SEQ"
    SCRNA_SEQ = "SCRNA_SEQ"
    OTHER = "OTHER"


class ColumnRole(StrEnum):
    UNKNOWN = "UNKNOWN"
    GENE_IDENTIFIER = "GENE_IDENTIFIER"
    P_VALUE = "P_VALUE"
    ADJUSTED_P_VALUE = "ADJUSTED_P_VALUE"
    EFFECT = "EFFECT"
    MEASUREMENT = "MEASUREMENT"
    OTHER = "OTHER"


class ColumnMapping(StructuralModel):
    """A caller's declaration, not a validated scientific interpretation."""

    column: Name
    role: ColumnRole
    definition: Name | None = None

    @field_validator("column", "definition")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("mapping text must not be blank")
        return value


class ColumnInspection(StructuralModel):
    column: Name
    tentative_role: ColumnRole
    blank_value_count: Count
    declared_missing_value_count: Count
    possible_missing_marker_count: Count
    # Counts lexical parseability only; no typed cells or values are exported.
    # Identifier columns are never tested for numerical conversion.
    numeric_like_value_count: Count | None
    missing_tokens: tuple[str, ...] = ()


class MappingQuestion(StructuralModel):
    code: Name
    columns: tuple[str, ...] = ()
    question: Name


class InspectionWarning(StructuralModel):
    code: Name
    columns: tuple[str, ...] = ()
    message: Name


class RowLocator(StructuralModel):
    logical_record: Annotated[int, Field(strict=True, ge=1)]
    physical_line_end: Annotated[int, Field(strict=True, ge=1)]


class DatasetManifest(StructuralModel):
    schema_version: Literal["dataset-manifest-v1"] = MANIFEST_SCHEMA_VERSION
    dataset_id: str
    inspection_id: str
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_size: Count
    source_scope: Literal["USER"] = "USER"
    input_format: Literal["CSV", "TSV"]
    delimiter: Literal[",", "\t"]
    encoding: Literal["utf-8-sig", "utf-8", "utf-16"]
    row_count: Count
    column_count: Count
    ordered_column_names: tuple[Name, ...]
    columns: tuple[ColumnInspection, ...]
    explicit_mappings: tuple[ColumnMapping, ...] = ()
    mapping_semantics: Literal["USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"] = (
        "USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"
    )
    unresolved_mapping_questions: tuple[MappingQuestion, ...]
    dataset_type: DatasetType = DatasetType.UNKNOWN
    dataset_type_basis: Literal["NOT_ESTABLISHED", "USER_DECLARED"]
    warnings: tuple[InspectionWarning, ...] = ()
    inspected_at: datetime
    blank_record_count: Count = 0
    header_confirmed_by_caller: bool = False
    missing_token_matching: Literal["STRIPPED_CASE_INSENSITIVE"] = (
        "STRIPPED_CASE_INSENSITIVE"
    )

    @field_validator("inspected_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("inspection timestamp requires a timezone")
        return value

    def identity_payload(self) -> dict:
        """Reproducible inspection content; operational time is excluded."""
        return self.model_dump(mode="json", exclude={"inspected_at", "inspection_id"})

    @staticmethod
    def content_digest(payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def consistent_manifest(self) -> "DatasetManifest":
        names = self.ordered_column_names
        if not names or len(names) != self.column_count:
            raise ValueError("column count must match a nonempty ordered header")
        stripped = [name.strip() for name in names]
        if any(not name for name in stripped) or len(set(stripped)) != len(names):
            raise ValueError("header names must be nonblank and unique")
        if tuple(column.column for column in self.columns) != names:
            raise ValueError("column inspections must match header order")
        mapped = [mapping.column for mapping in self.explicit_mappings]
        if len(set(mapped)) != len(mapped) or not set(mapped) <= set(names):
            raise ValueError("explicit mappings must name unique actual columns")
        if mapped != sorted(mapped, key=names.index):
            raise ValueError("explicit mappings must follow header order")
        for column in self.columns:
            identifier = column.tentative_role is ColumnRole.GENE_IDENTIFIER or any(
                mapping.column == column.column and mapping.role is ColumnRole.GENE_IDENTIFIER
                for mapping in self.explicit_mappings
            )
            if identifier and column.numeric_like_value_count is not None:
                raise ValueError("identifier columns must not have numeric profiling")
            if column.blank_value_count + column.declared_missing_value_count > self.row_count:
                raise ValueError("missing counts exceed row count")
            available = self.row_count - column.blank_value_count - column.declared_missing_value_count
            if column.possible_missing_marker_count > available:
                raise ValueError("possible missing counts exceed available values")
            if column.numeric_like_value_count is not None and column.numeric_like_value_count > available:
                raise ValueError("numeric-like count exceeds available values")
        for item in (*self.warnings, *self.unresolved_mapping_questions):
            if not set(item.columns) <= set(names):
                raise ValueError("diagnostic names an absent column")
        if self.delimiter != ("," if self.input_format == "CSV" else "\t"):
            raise ValueError("delimiter does not match input format")
        expected_basis = "NOT_ESTABLISHED" if self.dataset_type is DatasetType.UNKNOWN else "USER_DECLARED"
        if self.dataset_type_basis != expected_basis:
            raise ValueError("dataset type basis is inconsistent")
        if self.dataset_id != "DATASET_" + self.source_file_sha256:
            raise ValueError("dataset identity must match source bytes")
        if self.inspection_id != "INSPECTION_" + self.content_digest(self.identity_payload()):
            raise ValueError("inspection identity does not match manifest content")
        return self


class TableInspection(StructuralModel):
    manifest: DatasetManifest
    summary: str
    row_locators: tuple[RowLocator, ...]

    @model_validator(mode="after")
    def consistent_locators(self) -> "TableInspection":
        if len(self.row_locators) != self.manifest.row_count:
            raise ValueError("row locators must match data row count")
        if tuple(item.logical_record for item in self.row_locators) != tuple(range(1, self.manifest.row_count + 1)):
            raise ValueError("logical record locators must be consecutive")
        lines = [item.physical_line_end for item in self.row_locators]
        if lines != sorted(set(lines)):
            raise ValueError("physical line locators must be increasing")
        return self

    @property
    def unresolved_mapping_questions(self) -> tuple[MappingQuestion, ...]:
        return self.manifest.unresolved_mapping_questions
