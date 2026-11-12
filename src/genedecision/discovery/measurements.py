"""Same-snapshot measurement string/Decimal audit, with no scientific filtering."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .categories import _deep_validate
from .contracts import ColumnContract, Declaration, Digest, LocalMappingReference, MappingBinding, Text
from .identifiers import (
    Count, IdentifierInspection, IdentifierRecord, Status, _build_identifier_inspection,
    _column_mappings, _declaration_json,
)
from .measurement_contracts import (
    MAX_EXPONENT_MAGNITUDE, MAX_SIGNIFICANT_DIGITS, MAX_TOKEN_CHARACTERS,
    MeasurementAuditContract, MeasurementPolicy, RawText,
)
from .models import DatasetManifest
from .tables import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, DatasetInspectionError, _inspect_table_with_rows

ASCII_WHITESPACE = " \t\r\n\v\f"
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)
_NONFINITE = re.compile(r"[+-]?(?:inf(?:inity)?|s?nan[0-9]*)", re.ASCII | re.IGNORECASE)
ParseStatus = Literal["BLANK", "DECLARED_MISSING_TOKEN", "FINITE_DECIMAL", "NONFINITE_TOKEN", "NON_NUMERIC", "PARSER_LIMIT"]
PARSE_STATUSES = ("BLANK", "DECLARED_MISSING_TOKEN", "FINITE_DECIMAL", "NONFINITE_TOKEN", "NON_NUMERIC", "PARSER_LIMIT")
DEFAULT_MAX_CELLS = 1_000_000


class MeasurementAuditError(DatasetInspectionError):
    def __init__(self, code: str):
        super().__init__(code, "local measurement audit failed")


class ExactDecimal(Declaration):
    """Decimal.as_tuple() fields; preserves sign/scale without float or arithmetic."""

    sign: Annotated[int, Field(strict=True, ge=0, le=1)]
    digits: Annotated[tuple[Annotated[int, Field(strict=True, ge=0, le=9)], ...], Field(min_length=1, max_length=MAX_SIGNIFICANT_DIGITS)]
    exponent: Annotated[int, Field(strict=True, ge=-MAX_EXPONENT_MAGNITUDE, le=MAX_EXPONENT_MAGNITUDE)]


class MeasurementCell(Declaration):
    measurement_id: Text
    column: Text
    raw_value: RawText
    parse_input: RawText
    status: ParseStatus
    exact_decimal: ExactDecimal | None


def _observe(raw, policy):
    token = raw.strip(ASCII_WHITESPACE) if policy.strip_ascii_whitespace else raw
    status, exact = "NON_NUMERIC", None
    if len(raw) > MAX_TOKEN_CHARACTERS:
        status = "PARSER_LIMIT"
    elif not raw.strip(ASCII_WHITESPACE):
        status = "BLANK"
    elif raw in policy.missing_tokens:
        status = "DECLARED_MISSING_TOKEN"
    elif _NONFINITE.fullmatch(token):
        status = "NONFINITE_TOKEN"
    elif _NUMBER.fullmatch(token):
        coefficient, separator, exponent = token.lower().partition("e")
        digits = coefficient.lstrip("+-").replace(".", "").lstrip("0")
        exponent_digits = exponent.lstrip("+-").lstrip("0") or "0"
        if (len(digits) > MAX_SIGNIFICANT_DIGITS
                or len(exponent_digits) > len(str(MAX_EXPONENT_MAGNITUDE))
                or (separator and int(exponent_digits) > MAX_EXPONENT_MAGNITUDE)):
            status = "PARSER_LIMIT"
        else:
            try:
                # Construction/as_tuple do not round to the active Decimal
                # precision. No normalization, unary plus or arithmetic occurs.
                value = Decimal(token).as_tuple()
                if abs(value.exponent) > MAX_EXPONENT_MAGNITUDE:
                    status = "PARSER_LIMIT"
                else:
                    status = "FINITE_DECIMAL"
                    exact = ExactDecimal(sign=value.sign, digits=value.digits, exponent=value.exponent)
            except (InvalidOperation, ValueError, OverflowError):
                status = "NON_NUMERIC"
    return MeasurementCell(measurement_id=policy.measurement_id, column=policy.column,
                           raw_value=raw, parse_input=token, status=status, exact_decimal=exact)


def _check_binding(column_contract, audit_contract):
    if column_contract.content_sha256 != audit_contract.column_contract_sha256:
        raise MeasurementAuditError("MEASUREMENT_COLUMN_BINDING_MISMATCH")
    if {p.column for p in audit_contract.policies} != {m.column for m in column_contract.measurements}:
        raise MeasurementAuditError("MEASUREMENT_POLICY_COVERAGE_MISMATCH")


class MeasurementAuditRow(Declaration):
    identifier_record: IdentifierRecord
    cells: tuple[MeasurementCell, ...]


class MeasurementInputBinding(Declaration):
    dataset_id: Text
    source_file_sha256: Digest
    source_file_size: Count
    inspection_id: Text
    processing_id: Text
    column_contract_sha256: Digest
    mapping_binding: MappingBinding | None


def _binding(identifiers):
    manifest = identifiers.inspection.manifest
    return MeasurementInputBinding(
        dataset_id=manifest.dataset_id, source_file_sha256=manifest.source_file_sha256,
        source_file_size=manifest.source_file_size, inspection_id=manifest.inspection_id,
        processing_id=identifiers.processing_id, column_contract_sha256=identifiers.contract_sha256,
        mapping_binding=identifiers.column_contract.mapping_reference,
    )


class MeasurementAuditCounts(Declaration):
    input_row_count: Count
    measurement_column_count: Count
    cell_count: Count
    cell_denominator: Literal["INPUT_ROWS_TIMES_SELECTED_MEASUREMENTS"] = "INPUT_ROWS_TIMES_SELECTED_MEASUREMENTS"
    cell_status_counts: dict[ParseStatus, Count]
    identity_status_counts: dict[Status, Count]
    raw_duplicate_row_count: Count
    lexical_duplicate_row_count: Count
    ambiguous_row_count: Count
    blank_record_count: Count


def _counts(identifiers, rows, audit_contract):
    cell_counts = Counter(c.status for row in rows for c in row.cells)
    records = identifiers.identifiers
    return MeasurementAuditCounts(
        input_row_count=len(rows), measurement_column_count=len(audit_contract.policies),
        cell_count=sum(cell_counts.values()), cell_status_counts={s: cell_counts[s] for s in PARSE_STATUSES},
        identity_status_counts=dict(sorted(Counter(r.status for r in records).items())),
        raw_duplicate_row_count=sum(r.raw_occurrence_count > 1 for r in records),
        lexical_duplicate_row_count=sum(r.lexical_occurrence_count > 1 for r in records),
        ambiguous_row_count=sum(r.status == "UNRESOLVED_AMBIGUOUS" for r in records),
        blank_record_count=identifiers.inspection.manifest.blank_record_count,
    )


class MeasurementAuditResult(Declaration):
    schema_version: Literal["discovery-measurement-audit-v1"] = "discovery-measurement-audit-v1"
    data_classification: Literal["LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"] = "LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"
    declaration_semantics: Literal["USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"] = "USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"
    scientific_validation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    creation_validation_level: Literal["FRESH_SNAPSHOT_RECORDING"] = "FRESH_SNAPSHOT_RECORDING"
    identifiers: IdentifierInspection
    input_binding: MeasurementInputBinding
    audit_contract: MeasurementAuditContract
    audit_contract_sha256: Digest
    rows: tuple[MeasurementAuditRow, ...]
    counts: MeasurementAuditCounts
    audited_at: datetime
    audit_id: Text

    @field_validator("audited_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MEASUREMENT_TIMEZONE_REQUIRED")
        return value

    def identity_payload(self):
        payload = self.model_dump(mode="json", exclude={"audit_id", "audited_at"})
        payload["identifiers"]["inspection"]["manifest"].pop("inspected_at")
        return payload

    @model_validator(mode="after")
    def consistent_capture(self):
        # Revalidate legacy nested instances even in a mixed model/Mapping input.
        identifiers = _deep_validate(IdentifierInspection, self.identifiers)
        rows = tuple(_deep_validate(MeasurementAuditRow, row) for row in self.rows)
        _check_binding(identifiers.column_contract, self.audit_contract)
        if self.input_binding != _binding(identifiers) or self.audit_contract_sha256 != self.audit_contract.content_sha256:
            raise ValueError("MEASUREMENT_INPUT_BINDING_CONFLICT")
        if tuple(r.identifier_record for r in rows) != identifiers.identifiers:
            raise ValueError("MEASUREMENT_ROW_LOCATOR_CONFLICT")
        policies = self.audit_contract.policies
        for row in rows:
            if tuple((c.measurement_id, c.column) for c in row.cells) != tuple((p.measurement_id, p.column) for p in policies):
                raise ValueError("MEASUREMENT_CELL_POLICY_CONFLICT")
            for cell, policy in zip(row.cells, policies):
                if cell != _observe(cell.raw_value, policy):
                    raise ValueError("MEASUREMENT_PARSE_REPLAY_CONFLICT")
        if self.counts != _counts(identifiers, rows, self.audit_contract):
            raise ValueError("MEASUREMENT_COUNTS_CONFLICT")
        if self.audit_id != "MEASUREMENTS_" + DatasetManifest.content_digest(self.identity_payload()):
            raise ValueError("MEASUREMENT_IDENTITY_CONFLICT")
        return self

    def safe_summary(self):
        """Fixed statuses/counts; real-data summaries still must remain local."""
        return dict(schema_version=self.schema_version, data_classification=self.data_classification,
                    declaration_semantics=self.declaration_semantics, scientific_validation=self.scientific_validation,
                    **self.counts.model_dump(mode="json"))


def _load(text, model, code):
    try:
        return _deep_validate(model, _declaration_json(text))
    except (ValueError, TypeError, RecursionError):
        raise MeasurementAuditError(code) from None


def load_measurement_audit_contract(text: str | bytes) -> MeasurementAuditContract:
    return _load(text, MeasurementAuditContract, "INVALID_MEASUREMENT_AUDIT_CONTRACT")


def load_measurement_audit_result(text: str | bytes) -> MeasurementAuditResult:
    """Internal digest/parse replay only; no source byte or reference verification."""
    return _load(text, MeasurementAuditResult, "INVALID_MEASUREMENT_AUDIT_RESULT")


def inspect_measurements(
    path: str | Path, column_contract: ColumnContract | Mapping,
    audit_contract: MeasurementAuditContract | Mapping, *,
    mapping_reference: LocalMappingReference | Mapping | None = None,
    input_format: str | None = None, encoding: str = "utf-8-sig", confirm_header: bool = False,
    inspected_at: datetime | None = None, max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS, max_cells: int = DEFAULT_MAX_CELLS,
) -> MeasurementAuditResult:
    """Capture every selected raw cell alongside identities from one 6B1 snapshot."""
    if type(max_cells) is not int or max_cells < 1:
        raise MeasurementAuditError("INVALID_MEASUREMENT_CELL_LIMIT")
    try:
        column = _deep_validate(ColumnContract, column_contract)
    except (ValueError, TypeError, RecursionError):
        raise MeasurementAuditError("INVALID_COLUMN_CONTRACT") from None
    try:
        audit = _deep_validate(MeasurementAuditContract, audit_contract)
    except (ValueError, TypeError, RecursionError):
        raise MeasurementAuditError("INVALID_MEASUREMENT_AUDIT_CONTRACT") from None
    try:
        reference = _deep_validate(LocalMappingReference, mapping_reference) if mapping_reference is not None else None
    except (ValueError, TypeError, RecursionError):
        raise MeasurementAuditError("INVALID_MAPPING_REFERENCE") from None
    _check_binding(column, audit)
    if reference is not None and reference.binding != column.mapping_reference:
        raise MeasurementAuditError("MAPPING_REFERENCE_BINDING_MISMATCH")
    try:
        inspection, raw_rows = _inspect_table_with_rows(
            path, mappings=_column_mappings(column), input_format=input_format, encoding=encoding,
            confirm_header=confirm_header, inspected_at=inspected_at, max_bytes=max_bytes, max_rows=max_rows,
        )
        if len(raw_rows) * len(audit.policies) > max_cells:
            raise MeasurementAuditError("MEASUREMENT_CELL_LIMIT")
        identifiers = _build_identifier_inspection(inspection, raw_rows, column, reference)
        rows = tuple(MeasurementAuditRow(
            identifier_record=identifier,
            cells=tuple(_observe(raw.values[p.column], p) for p in audit.policies),
        ) for identifier, raw in zip(identifiers.identifiers, raw_rows))
        payload = dict(identifiers=identifiers, input_binding=_binding(identifiers),
                       audit_contract=audit, audit_contract_sha256=audit.content_sha256,
                       rows=rows, counts=_counts(identifiers, rows, audit), audited_at=datetime.now(timezone.utc))
        provisional = MeasurementAuditResult.model_construct(**payload, audit_id="pending")
        payload["audit_id"] = "MEASUREMENTS_" + DatasetManifest.content_digest(provisional.identity_payload())
        return MeasurementAuditResult(**payload)
    except DatasetInspectionError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise MeasurementAuditError("MEASUREMENT_AUDIT_FAILED") from None


def verify_measurement_source(path: str | Path, result: MeasurementAuditResult | Mapping, *,
                              mapping_reference: LocalMappingReference | Mapping | None = None,
                              max_bytes: int = DEFAULT_MAX_BYTES, max_rows: int = DEFAULT_MAX_ROWS,
                              max_cells: int = DEFAULT_MAX_CELLS):
    """Process fresh bytes under the frozen contracts; return only a matching audit.

    Internal loading alone cannot establish that serialized cells came from the
    bound source. This operation reads a new snapshot and compares its full ID.
    """
    try:
        prior = _deep_validate(MeasurementAuditResult, result)
    except (ValueError, TypeError, RecursionError):
        raise MeasurementAuditError("INVALID_MEASUREMENT_AUDIT_RESULT") from None
    if prior.identifiers.reference_supplied != (mapping_reference is not None):
        raise MeasurementAuditError("MEASUREMENT_REFERENCE_SUPPLY_MISMATCH")
    manifest = prior.identifiers.inspection.manifest
    fresh = inspect_measurements(
        path, prior.identifiers.column_contract, prior.audit_contract,
        mapping_reference=mapping_reference, input_format=manifest.input_format,
        encoding=manifest.encoding, confirm_header=manifest.header_confirmed_by_caller,
        max_bytes=max_bytes, max_rows=max_rows, max_cells=max_cells,
    )
    if fresh.audit_id != prior.audit_id:
        raise MeasurementAuditError("MEASUREMENT_SOURCE_REPLAY_MISMATCH")
    return fresh
