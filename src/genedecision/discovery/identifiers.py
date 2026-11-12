"""Local identifier auditing bound to the same byte snapshot as inspection."""

from __future__ import annotations

import json

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import ColumnContract, Declaration, LocalMappingReference
from .models import ColumnMapping, ColumnRole, DatasetManifest, RowLocator, TableInspection
from .tables import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, DatasetInspectionError, _inspect_table_with_rows

Status = Literal[
    "BLANK", "DECLARED_MISSING", "EMPTY_AFTER_POLICY", "UNRESOLVED_NO_REFERENCE",
    "UNRESOLVED_REFERENCE_NOT_SUPPLIED", "UNRESOLVED_NOT_FOUND",
    "UNRESOLVED_AMBIGUOUS", "RESOLVED_BY_LOCAL_REFERENCE",
]
Count = Annotated[int, Field(strict=True, ge=0)]


class IdentifierProcessingError(DatasetInspectionError):
    """Only fixed codes/messages leave the public processing boundary."""

    def __init__(self, code: str):
        super().__init__(code, "local declaration or identifier processing failed")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            # Never include the key/value in this error, even internally.
            raise ValueError("DUPLICATE_JSON_KEY")
        obj[key] = value
    return obj


def _reject_json_constant(value: str):
    raise ValueError("NONSTANDARD_JSON_CONSTANT")


def _declaration_json(text: str | bytes):
    # object_pairs_hook runs for every nested object before duplicate keys can
    # be discarded. Restrict declaration bytes to UTF-8 rather than guessing.
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    return json.loads(text, object_pairs_hook=_unique_json_object,
                      parse_constant=_reject_json_constant)


def load_column_contract(text: str | bytes) -> ColumnContract:
    try:
        return ColumnContract.model_validate(_declaration_json(text))
    except (ValueError, TypeError, RecursionError):
        raise IdentifierProcessingError("INVALID_COLUMN_CONTRACT") from None


def load_mapping_reference(text: str | bytes) -> LocalMappingReference:
    try:
        return LocalMappingReference.model_validate(_declaration_json(text))
    except (ValueError, TypeError, RecursionError):
        raise IdentifierProcessingError("INVALID_MAPPING_REFERENCE") from None


class IdentifierRecord(Declaration):
    locator: RowLocator
    raw_identifier: Annotated[str, Field(strict=True)]
    lexical_identifier: Annotated[str, Field(strict=True)] | None
    raw_occurrence_count: Annotated[int, Field(strict=True, ge=1)]
    lexical_occurrence_count: Count
    status: Status
    # All distinct targets retained; ambiguous results are never selected.
    mapping_targets: tuple[Annotated[str, Field(strict=True, min_length=1)], ...] = ()
    mapping_entry_count: Count = 0


class IdentifierInspection(Declaration):
    schema_version: Literal["discovery-identifiers-v1"] = "discovery-identifiers-v1"
    data_classification: Literal["LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"] = "LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"
    inspection: TableInspection
    column_contract: ColumnContract
    contract_sha256: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
    reference_supplied: Annotated[bool, Field(strict=True)]
    identifiers: tuple[IdentifierRecord, ...]
    processing_id: str

    def identity_payload(self) -> dict:
        return dict(schema_version=self.schema_version,
                    data_classification=self.data_classification,
                    inspection_id=self.inspection.manifest.inspection_id,
                    contract_sha256=self.contract_sha256,
                    reference_supplied=self.reference_supplied,
                    identifiers=[r.model_dump(mode="json") for r in self.identifiers])

    @model_validator(mode="after")
    def consistent_result(self) -> "IdentifierInspection":
        contract = self.column_contract
        if self.contract_sha256 != contract.content_sha256:
            raise ValueError("RESULT_CONTRACT_DIGEST_MISMATCH")
        manifest = self.inspection.manifest
        expected = _column_mappings(contract)
        expected = tuple(sorted(expected, key=lambda m: manifest.ordered_column_names.index(m.column)))
        if manifest.explicit_mappings != expected:
            raise ValueError("RESULT_MAPPING_CONFLICT")
        if tuple(r.locator for r in self.identifiers) != self.inspection.row_locators:
            raise ValueError("RESULT_LOCATOR_CONFLICT")
        if self.reference_supplied and contract.mapping_reference is None:
            raise ValueError("RESULT_REFERENCE_CONFLICT")
        raw_counts = Counter(r.raw_identifier for r in self.identifiers)
        lexical_counts = Counter(r.lexical_identifier for r in self.identifiers if r.lexical_identifier is not None)
        for record in self.identifiers:
            lexical, missing = _lexical_identifier(record.raw_identifier, contract)
            if record.lexical_identifier != lexical:
                raise ValueError("RESULT_LEXICAL_CONFLICT")
            if record.raw_occurrence_count != raw_counts[record.raw_identifier]:
                raise ValueError("RESULT_DUPLICATE_COUNT_CONFLICT")
            if record.lexical_occurrence_count != (lexical_counts[lexical] if lexical is not None else 0):
                raise ValueError("RESULT_DUPLICATE_COUNT_CONFLICT")
            targets = record.mapping_targets
            if targets != tuple(sorted(set(targets))) or record.mapping_entry_count < len(targets):
                raise ValueError("RESULT_MAPPING_TARGET_CONFLICT")
            if missing:
                expected_status = missing
            elif not contract.mapping_reference:
                expected_status = "UNRESOLVED_NO_REFERENCE"
            elif not self.reference_supplied:
                expected_status = "UNRESOLVED_REFERENCE_NOT_SUPPLIED"
            elif not targets:
                expected_status = "UNRESOLVED_NOT_FOUND"
            elif len(targets) > 1:
                expected_status = "UNRESOLVED_AMBIGUOUS"
            else:
                expected_status = "RESOLVED_BY_LOCAL_REFERENCE"
            if record.status != expected_status:
                raise ValueError("RESULT_STATUS_CONFLICT")
            if (missing or not self.reference_supplied) and (targets or record.mapping_entry_count):
                raise ValueError("RESULT_UNEXPECTED_MAPPING")
        expected_id = "IDENTIFIERS_" + DatasetManifest.content_digest(self.identity_payload())
        if self.processing_id != expected_id:
            raise ValueError("RESULT_IDENTITY_MISMATCH")
        return self

    def safe_summary(self) -> dict:
        """Counts/status only; no headers, declarations, paths, hashes or cells.

        This limits displayed fields, not disclosure policy. Aggregates from real
        data are unpublished research results: keep them local/OpenAI, never
        upload to GitHub or third parties without specific authorization.
        """
        counts = Counter(r.status for r in self.identifiers)
        return dict(schema_version=self.schema_version,
                    declaration_status="USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED",
                    reference_status="SUPPLIED_USER_DECLARED" if self.reference_supplied else "NOT_SUPPLIED",
                    scientific_validation="NOT_PERFORMED",
                    row_count=len(self.identifiers),
                    column_count=self.inspection.manifest.column_count,
                    measurement_column_count=len(self.column_contract.measurements),
                    blank_record_count=self.inspection.manifest.blank_record_count,
                    raw_duplicate_row_count=sum(r.raw_occurrence_count > 1 for r in self.identifiers),
                    lexical_duplicate_row_count=sum(r.lexical_occurrence_count > 1 for r in self.identifiers),
                    status_counts=dict(sorted(counts.items())))


def _column_mappings(contract: ColumnContract) -> tuple[ColumnMapping, ...]:
    return tuple(ColumnMapping(column=m.column, role=ColumnRole(m.role), definition=m.definition)
                 for m in (contract.primary_gene, *contract.measurements))


def _lexical_identifier(raw: str, contract: ColumnContract) -> tuple[str | None, str | None]:
    if not raw.strip():
        return None, "BLANK"
    if raw in contract.missing_identifier_tokens:
        return None, "DECLARED_MISSING"
    value = contract.lexical_policy.apply(raw)
    if not value:
        return None, "EMPTY_AFTER_POLICY"
    return value, None


def _build_identifier_inspection(inspection, rows, declared, reference):
    """Pure 6B2 construction from already-frozen rows; no source reads."""
    values = [row.values[declared.primary_gene.column] for row in rows]
    raw_counts = Counter(values)
    lexicals = [_lexical_identifier(raw, declared) for raw in values]
    lexical_counts = Counter(value for value, _ in lexicals if value is not None)
    index = defaultdict(list)
    if reference:
        for entry in reference.entries:
            index[entry.source].append(entry.target)
    records = []
    for locator, raw, (lexical, missing) in zip(inspection.row_locators, values, lexicals):
        hits = index.get(lexical, []) if missing is None else []
        targets = tuple(sorted(set(hits)))
        if missing:
            status = missing
        elif declared.mapping_reference is None:
            status = "UNRESOLVED_NO_REFERENCE"
        elif reference is None:
            status = "UNRESOLVED_REFERENCE_NOT_SUPPLIED"
        elif not targets:
            status = "UNRESOLVED_NOT_FOUND"
        elif len(targets) > 1:
            status = "UNRESOLVED_AMBIGUOUS"
        else:
            status = "RESOLVED_BY_LOCAL_REFERENCE"
        records.append(IdentifierRecord(
            locator=locator, raw_identifier=raw, lexical_identifier=lexical,
            raw_occurrence_count=raw_counts[raw],
            lexical_occurrence_count=lexical_counts[lexical] if lexical is not None else 0,
            status=status, mapping_targets=targets, mapping_entry_count=len(hits),
        ))
    identity = dict(schema_version="discovery-identifiers-v1",
                    data_classification="LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD",
                    inspection_id=inspection.manifest.inspection_id,
                    contract_sha256=declared.content_sha256,
                    reference_supplied=reference is not None,
                    identifiers=[r.model_dump(mode="json") for r in records])
    return IdentifierInspection(
        inspection=inspection, column_contract=declared,
        contract_sha256=declared.content_sha256, reference_supplied=reference is not None,
        identifiers=tuple(records),
        processing_id="IDENTIFIERS_" + DatasetManifest.content_digest(identity),
    )


def inspect_identifiers(
    path: str | Path,
    contract: ColumnContract | Mapping,
    *,
    mapping_reference: LocalMappingReference | Mapping | None = None,
    input_format: str | None = None,
    encoding: str = "utf-8-sig",
    confirm_header: bool = False,
    inspected_at: datetime | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> IdentifierInspection:
    """Preserve every data record; returned raw identifiers are local research data.

    No reference is opened implicitly. Exact reference keys match the explicitly
    policy-derived string; no namespace/taxon/alias/version inference occurs.
    """
    try:
        declared = ColumnContract.model_validate(contract)
    except (ValueError, TypeError):
        raise IdentifierProcessingError("INVALID_COLUMN_CONTRACT") from None
    try:
        reference = (LocalMappingReference.model_validate(mapping_reference)
                     if mapping_reference is not None else None)
    except (ValueError, TypeError):
        raise IdentifierProcessingError("INVALID_MAPPING_REFERENCE") from None
    if reference and reference.binding != declared.mapping_reference:
        raise IdentifierProcessingError("MAPPING_REFERENCE_BINDING_MISMATCH")
    try:
        inspection, rows = _inspect_table_with_rows(
            path, mappings=_column_mappings(declared), input_format=input_format,
            encoding=encoding, confirm_header=confirm_header, inspected_at=inspected_at,
            max_bytes=max_bytes, max_rows=max_rows,
        )
        # No second source read: rows belong to the bytes hashed in this manifest.
        return _build_identifier_inspection(inspection, rows, declared, reference)
    except DatasetInspectionError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise IdentifierProcessingError("IDENTIFIER_PROCESSING_FAILED") from None
