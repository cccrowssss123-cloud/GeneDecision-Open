"""Single-table exploratory filtering and exact mapped-candidate aggregation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator

from .categories import (
    EvaluationResult, _combine, _deep_validate, _evaluate as _evaluate_categories,
    _replay_mapping, replay_evaluation_result,
)
from .category_contracts import AnnotationReference, CategoryCondition
from .contracts import ColumnContract, Declaration, Digest, LocalMappingReference, Text
from .identifiers import IdentifierInspection, _build_identifier_inspection, _column_mappings
from .measurement_contracts import MeasurementAuditContract, RawText
from .measurements import DEFAULT_MAX_CELLS, _build_measurement_audit, _check_binding
from .models import DatasetManifest, RowLocator
from .numeric import DEFAULT_MAX_EVALUATIONS, NumericEvaluationResult, evaluate_numeric_conditions
from .numeric_contracts import NumericCondition, NumericContext
from .tables import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, DatasetInspectionError, _inspect_table_with_rows

Truth = Literal["T", "F", "U"]
Combine = Literal["ALL", "ANY"]


class DiscoveryRunError(DatasetInspectionError):
    """A fixed-code run failure; raw table or declaration content stays local."""

    def __init__(self, code: str, *, row_number: int | None = None):
        super().__init__(code, "local discovery run failed", row_number=row_number)


class CandidateKey(Declaration):
    taxon: Text
    namespace: Text
    identifier: Text

    @field_validator("identifier")
    @classmethod
    def nonblank_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DISCOVERY_CANDIDATE_IDENTIFIER_BLANK")
        return value


class DiscoveryObservation(Declaration):
    locator: RowLocator
    raw_cells: tuple[RawText, ...]
    category_truth: Truth | None
    numeric_truth: Truth | None
    truth: Truth
    canonical_key: CandidateKey | None


class DiscoveryCandidate(Declaration):
    key: CandidateKey
    matching_rows: tuple[RowLocator, ...]
    observed_rows: tuple[RowLocator, ...]


def _check_combination(category_present, numeric_present, combine):
    if not category_present and not numeric_present:
        raise DiscoveryRunError("DISCOVERY_CONDITION_REQUIRED")
    if category_present and numeric_present:
        if combine not in ("ALL", "ANY"):
            raise DiscoveryRunError("DISCOVERY_COMBINE_REQUIRED")
    elif combine is not None:
        raise DiscoveryRunError("DISCOVERY_COMBINE_NOT_APPLICABLE")


def _key(record, reference):
    if reference is None or record.status != "RESOLVED_BY_LOCAL_REFERENCE":
        return None
    return CandidateKey(taxon=reference.taxon, namespace=reference.target_namespace,
                        identifier=record.mapping_targets[0])


def _observation(record, raw_cells, category, numeric, combine, reference):
    values = [value for value in (category, numeric) if value is not None]
    truth = _combine(values, combine) if combine is not None else values[0]
    return DiscoveryObservation(locator=record.locator, raw_cells=raw_cells, category_truth=category,
                                numeric_truth=numeric, truth=truth, canonical_key=_key(record, reference))


def _aggregate(observations):
    # Dict insertion order keeps the first occurrence of each exact mapped key.
    observed = {}
    matching = {}
    for row in observations:
        if row.canonical_key is None:
            continue
        observed.setdefault(row.canonical_key, []).append(row.locator)
        if row.truth == "T":
            matching.setdefault(row.canonical_key, []).append(row.locator)
    return tuple(DiscoveryCandidate(key=key, matching_rows=tuple(matching[key]), observed_rows=tuple(locators))
                 for key, locators in observed.items() if key in matching)


class DiscoveryRun(Declaration):
    schema_version: Literal["discovery-run-v1"] = "discovery-run-v1"
    data_classification: Literal["LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"] = "LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"
    scientific_validation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    warning: Literal["SCIENTIFICALLY_UNVERIFIED"] = "SCIENTIFICALLY_UNVERIFIED"
    readiness: Literal["NOT_ASSESSED"] = "NOT_ASSESSED"
    identifiers: IdentifierInspection
    mapping_reference: LocalMappingReference | None
    observations: tuple[DiscoveryObservation, ...]
    category_evaluation: EvaluationResult | None
    numeric_evaluation: NumericEvaluationResult | None
    candidates: tuple[DiscoveryCandidate, ...]
    combine: Combine | None
    run_id: Digest

    def identity_payload(self) -> dict:
        """Bind the content identities, every raw/derived row, and candidate lists.

        The digest checks internal consistency; it cannot authenticate source bytes.
        """
        category, numeric, reference = self.category_evaluation, self.numeric_evaluation, self.mapping_reference
        return dict(schema_version=self.schema_version, data_classification=self.data_classification,
                    scientific_validation=self.scientific_validation, warning=self.warning, readiness=self.readiness,
                    processing_id=self.identifiers.processing_id, combine=self.combine,
                    mapping_sha256=reference.content_sha256 if reference is not None else None,
                    category_evaluation_id=category.evaluation_id if category is not None else None,
                    numeric_evaluation_id=numeric.evaluation_id if numeric is not None else None,
                    observations=[row.model_dump(mode="json") for row in self.observations],
                    candidates=[item.model_dump(mode="json") for item in self.candidates])

    @model_validator(mode="after")
    def validate_consistency(self):
        identifiers = _deep_validate(IdentifierInspection, self.identifiers)
        category, numeric, reference = self.category_evaluation, self.numeric_evaluation, self.mapping_reference
        _check_combination(category is not None, numeric is not None, self.combine)
        if identifiers.reference_supplied != (reference is not None):
            raise ValueError("DISCOVERY_MAPPING_SUPPLY_MISMATCH")
        if reference is not None:
            if identifiers.column_contract.mapping_reference != reference.binding:
                raise ValueError("DISCOVERY_MAPPING_BINDING_MISMATCH")
            _replay_mapping(identifiers, reference)
        if category is not None:
            # Recompute applicability and comparisons from the retained inputs;
            # a self-consistent digest alone is not a reference replay.
            category = replay_evaluation_result(
                category, annotation_reference=category.annotation_reference,
                conditions=category.conditions, mapping_reference=reference,
            )
            if category.identifiers != identifiers:
                raise ValueError("DISCOVERY_CATEGORY_INPUT_MISMATCH")
        if numeric is not None and numeric.measurement_audit.identifiers != identifiers:
            raise ValueError("DISCOVERY_NUMERIC_INPUT_MISMATCH")

        columns = identifiers.inspection.manifest.ordered_column_names
        index = {name: i for i, name in enumerate(columns)}
        gene_index = index[identifiers.column_contract.primary_gene.column]
        if len(self.observations) != len(identifiers.identifiers):
            raise ValueError("DISCOVERY_ROW_COUNT_MISMATCH")
        for i, (record, row) in enumerate(zip(identifiers.identifiers, self.observations)):
            # RowLocator predates revalidate_instances; a copied bool/float can
            # compare equal to the expected integer without being valid.
            _deep_validate(RowLocator, row.locator)
            if len(row.raw_cells) != len(columns) or row.raw_cells[gene_index] != record.raw_identifier:
                raise ValueError("DISCOVERY_RAW_IDENTIFIER_OR_WIDTH_MISMATCH")
            if numeric is not None and any(row.raw_cells[index[cell.column]] != cell.raw_value
                                           for cell in numeric.measurement_audit.rows[i].cells):
                raise ValueError("DISCOVERY_RAW_MEASUREMENT_MISMATCH")
            expected = _observation(record, row.raw_cells, category.rows[i].truth if category is not None else None,
                                    numeric.rows[i].truth if numeric is not None else None, self.combine, reference)
            if row != expected:
                raise ValueError("DISCOVERY_OBSERVATION_MISMATCH")
        for candidate in self.candidates:
            for locator in (*candidate.matching_rows, *candidate.observed_rows):
                _deep_validate(RowLocator, locator)
        if self.candidates != _aggregate(self.observations):
            raise ValueError("DISCOVERY_CANDIDATE_AGGREGATION_MISMATCH")
        if self.run_id != DatasetManifest.content_digest(self.identity_payload()):
            raise ValueError("DISCOVERY_RUN_ID_MISMATCH")
        return self


def run_discovery(
    path: str | Path, column_contract: ColumnContract | Mapping, *,
    mapping_reference: LocalMappingReference | Mapping | None = None,
    annotation_reference: AnnotationReference | Mapping | None = None,
    category_condition: CategoryCondition | Mapping | None = None,
    measurement_audit_contract: MeasurementAuditContract | Mapping | None = None,
    numeric_condition: NumericCondition | Mapping | None = None,
    numeric_context: NumericContext | Mapping | None = None,
    combine: Combine | None = None,
    input_format: str | None = None, encoding: str = "utf-8-sig", confirm_header: bool = False,
    inspected_at: datetime | None = None, expected_source_sha256: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS, max_cells: int = DEFAULT_MAX_CELLS,
    max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
) -> DiscoveryRun:
    """Read one local table once and retain all rows, including false/unknown rows."""
    has_category = category_condition is not None
    has_numeric = numeric_condition is not None
    if has_category != (annotation_reference is not None):
        raise DiscoveryRunError("DISCOVERY_CATEGORY_DECLARATIONS_INCOMPLETE")
    if has_numeric != (measurement_audit_contract is not None) or (numeric_context is not None and not has_numeric):
        raise DiscoveryRunError("DISCOVERY_NUMERIC_DECLARATIONS_INCOMPLETE")
    _check_combination(has_category, has_numeric, combine)
    if type(max_cells) is not int or max_cells < 1:
        raise DiscoveryRunError("INVALID_DISCOVERY_CELL_LIMIT")
    if expected_source_sha256 is not None and (
            type(expected_source_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256) is None):
        raise DiscoveryRunError("INVALID_DISCOVERY_SOURCE_SHA256")
    try:
        column = _deep_validate(ColumnContract, column_contract)
        reference = _deep_validate(LocalMappingReference, mapping_reference) if mapping_reference is not None else None
        if reference is not None and column.mapping_reference != reference.binding:
            raise DiscoveryRunError("DISCOVERY_MAPPING_BINDING_MISMATCH")
        annotation = _deep_validate(AnnotationReference, annotation_reference) if has_category else None
        category_declaration = _deep_validate(CategoryCondition, category_condition) if has_category else None
        audit_declaration = _deep_validate(MeasurementAuditContract, measurement_audit_contract) if has_numeric else None
        numeric_declaration = _deep_validate(NumericCondition, numeric_condition) if has_numeric else None
        context = _deep_validate(NumericContext, numeric_context) if numeric_context is not None else None
        if audit_declaration is not None:
            _check_binding(column, audit_declaration)
        inspection, raw_rows = _inspect_table_with_rows(
            path, mappings=_column_mappings(column), input_format=input_format, encoding=encoding,
            confirm_header=confirm_header, inspected_at=inspected_at, max_bytes=max_bytes, max_rows=max_rows,
        )
        if expected_source_sha256 is not None and inspection.manifest.source_file_sha256 != expected_source_sha256:
            raise DiscoveryRunError("DISCOVERY_SOURCE_SHA256_MISMATCH")
        columns = inspection.manifest.ordered_column_names
        if len(raw_rows) * len(columns) > max_cells:
            raise DiscoveryRunError("DISCOVERY_CELL_LIMIT")
        identifiers = _build_identifier_inspection(inspection, raw_rows, column, reference)
        category = (_evaluate_categories(identifiers, annotation, category_declaration, reference,
                                         "FRESH_SNAPSHOT_PROCESSING") if has_category else None)
        numeric = None
        if has_numeric:
            measurement = _build_measurement_audit(identifiers, raw_rows, audit_declaration,
                                                   audited_at=datetime.now(timezone.utc))
            numeric = evaluate_numeric_conditions(measurement, numeric_declaration, context=context,
                                                  max_evaluations=max_evaluations)
        observations = tuple(
            _observation(record, tuple(raw.values[name] for name in columns),
                         category.rows[i].truth if category is not None else None,
                         numeric.rows[i].truth if numeric is not None else None, combine, reference)
            for i, (record, raw) in enumerate(zip(identifiers.identifiers, raw_rows))
        )
        payload = dict(identifiers=identifiers, mapping_reference=reference, observations=observations,
                       category_evaluation=category, numeric_evaluation=numeric,
                       candidates=_aggregate(observations), combine=combine)
        provisional = DiscoveryRun.model_construct(**payload, run_id="")
        return DiscoveryRun(**payload, run_id=DatasetManifest.content_digest(provisional.identity_payload()))
    except DiscoveryRunError:
        raise
    except DatasetInspectionError as exc:
        raise DiscoveryRunError(exc.code, row_number=exc.row_number) from None
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise DiscoveryRunError("INVALID_DISCOVERY_INPUT_OR_RESULT") from None
