"""Exploratory row comparisons, always scientifically unverified."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .categories import _combine, _deep_validate, _replay_mapping
from .contracts import Declaration, LocalMappingReference, Text
from .identifiers import Count, IdentifierRecord, _declaration_json
from .measurements import DEFAULT_MAX_CELLS, MeasurementAuditResult, MeasurementCell, verify_measurement_source
from .models import DatasetManifest
from .numeric_contracts import NumericCondition, NumericContext, threshold_value
from .tables import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, DatasetInspectionError

Truth = Literal["T", "F", "U"]
Outcome = Literal["EXPLORATORY_NUMERIC_MATCH", "EXPLORATORY_NUMERIC_MATCH_QC_FLAGGED",
                  "EXPLORATORY_NUMERIC_NONMATCH", "NUMERIC_UNKNOWN"]
QCStatus = Literal["UNKNOWN", "DECLARED_PASS", "DECLARED_FAIL", "MIXED_DECLARATIONS"]
Reason = Literal["COMPARISON_PASSED", "COMPARISON_FAILED", "VALUE_UNAVAILABLE", "PREDICATE_UNDEFINED"]
DEFAULT_MAX_EVALUATIONS = 1_000_000


class NumericEvaluationError(DatasetInspectionError):
    def __init__(self, code):
        super().__init__(code, "local exploratory numeric evaluation failed")


def _validate_inputs(audit, condition, context):
    if condition.measurement_audit_id != audit.audit_id:
        raise NumericEvaluationError("NUMERIC_AUDIT_BINDING_MISMATCH")
    if condition.context_sha256 != (context.content_sha256 if context is not None else None):
        raise NumericEvaluationError("NUMERIC_CONTEXT_BINDING_MISMATCH")
    selected = {p.measurement_id for p in audit.audit_contract.policies}
    if not {p.measurement_id for p in condition.predicates} <= selected:
        raise NumericEvaluationError("NUMERIC_MEASUREMENT_NOT_SELECTED")
    if context is not None:
        if context.measurement_audit_id != audit.audit_id:
            raise NumericEvaluationError("NUMERIC_CONTEXT_AUDIT_MISMATCH")
        locators = {r.identifier_record.locator for r in audit.rows}
        for declaration in context.qc_declarations:
            if not set(declaration.measurement_ids) <= selected:
                raise NumericEvaluationError("NUMERIC_QC_MEASUREMENT_NOT_SELECTED")
            if not set(declaration.locators) <= locators:
                raise NumericEvaluationError("NUMERIC_QC_LOCATOR_NOT_PRESENT")


def _decimal(exact):
    return Decimal((exact.sign, exact.digits, exact.exponent))


class NumericPredicateResult(Declaration):
    predicate_id: Text
    cell: MeasurementCell
    truth: Truth
    reason: Reason
    qc_declaration_ids: tuple[Text, ...]
    qc_status: QCStatus
    has_declared_qc_failure: Annotated[bool, Field(strict=True)]


class NumericRowResult(Declaration):
    identifier_record: IdentifierRecord
    predicates: tuple[NumericPredicateResult, ...]
    truth: Truth
    outcome: Outcome
    decisive_predicate_ids: tuple[Text, ...]
    has_declared_qc_failure: Annotated[bool, Field(strict=True)]
    scientific_validation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    scientific_status: Literal["NOT_VALIDATED"] = "NOT_VALIDATED"
    scientific_hit_status: Literal["NOT_CONFIRMED"] = "NOT_CONFIRMED"
    warning: Literal["SCIENTIFICALLY_UNVERIFIED"] = "SCIENTIFICALLY_UNVERIFIED"


def _evaluate_rows(audit, condition, context):
    constants = {p.predicate_id: (_decimal(threshold_value(p.threshold).exact_decimal)
                                 if p.threshold is not None else None) for p in condition.predicates}
    qc_index = defaultdict(list)
    if context is not None:
        for declaration in context.qc_declarations:
            locator_set = frozenset(declaration.locators)
            for measurement_id in declaration.measurement_ids:
                qc_index[measurement_id].append((declaration, locator_set))
    results = []
    for row in audit.rows:
        cells = {c.measurement_id: c for c in row.cells}
        predicates = []
        for predicate in condition.predicates:
            cell = cells[predicate.measurement_id]
            if predicate.unresolved_reason is not None:
                truth, reason = "U", "PREDICATE_UNDEFINED"
            elif cell.status != "FINITE_DECIMAL":
                truth, reason = "U", "VALUE_UNAVAILABLE"
            else:
                left, right = _decimal(cell.exact_decimal), constants[predicate.predicate_id]
                # Finite Decimal construction/comparison does not round to the
                # current arithmetic context, infer units or transform scales.
                operator = predicate.operator
                passed = (left < right if operator == "LT" else left <= right if operator == "LE"
                          else left > right if operator == "GT" else left >= right if operator == "GE"
                          else left == right)
                truth, reason = ("T", "COMPARISON_PASSED") if passed else ("F", "COMPARISON_FAILED")
            applicable = [d for d, locators in qc_index[predicate.measurement_id]
                          if d.scope == "DATASET" or row.identifier_record.locator in locators]
            statuses = {d.status for d in applicable}
            qc_status = (next(iter(statuses)) if len(statuses) == 1 else
                         "MIXED_DECLARATIONS" if statuses else "UNKNOWN")
            predicates.append(NumericPredicateResult(
                predicate_id=predicate.predicate_id, cell=cell, truth=truth, reason=reason,
                qc_declaration_ids=tuple(d.declaration_id for d in applicable), qc_status=qc_status,
                has_declared_qc_failure="DECLARED_FAIL" in statuses,
            ))
        truth = _combine([p.truth for p in predicates], condition.combine)
        failure = any(p.has_declared_qc_failure for p in predicates)
        outcome = ("EXPLORATORY_NUMERIC_MATCH_QC_FLAGGED" if failure else "EXPLORATORY_NUMERIC_MATCH") if truth == "T" else (
            "EXPLORATORY_NUMERIC_NONMATCH" if truth == "F" else "NUMERIC_UNKNOWN")
        results.append(NumericRowResult(
            identifier_record=row.identifier_record, predicates=tuple(predicates), truth=truth, outcome=outcome,
            decisive_predicate_ids=tuple(p.predicate_id for p in predicates if p.truth == truth) if truth != "U" else (),
            has_declared_qc_failure=failure,
        ))
    return tuple(results)


class NumericCounts(Declaration):
    input_row_count: Count
    predicate_occurrence_count: Count
    row_truth_counts: dict[Truth, Count]
    predicate_truth_counts: dict[Truth, Count]
    outcome_counts: dict[Outcome, Count]
    qc_flagged_row_count: Count
    qc_flagged_match_count: Count
    raw_duplicate_row_count: Count
    ambiguous_identity_row_count: Count
    reason_counts: dict[Reason, Count]
    predicate_qc_status_counts: dict[QCStatus, Count]


def _counts(rows):
    predicates = [p for r in rows for p in r.predicates]
    return NumericCounts(
        input_row_count=len(rows), predicate_occurrence_count=len(predicates),
        row_truth_counts={v: sum(r.truth == v for r in rows) for v in ("T", "F", "U")},
        predicate_truth_counts=dict(sorted(Counter(p.truth for p in predicates).items())),
        outcome_counts={s: sum(r.outcome == s for r in rows) for s in (
            "EXPLORATORY_NUMERIC_MATCH", "EXPLORATORY_NUMERIC_MATCH_QC_FLAGGED", "EXPLORATORY_NUMERIC_NONMATCH", "NUMERIC_UNKNOWN")},
        qc_flagged_row_count=sum(r.has_declared_qc_failure for r in rows),
        qc_flagged_match_count=sum(r.truth == "T" and r.has_declared_qc_failure for r in rows),
        raw_duplicate_row_count=sum(r.identifier_record.raw_occurrence_count > 1 for r in rows),
        ambiguous_identity_row_count=sum(r.identifier_record.status == "UNRESOLVED_AMBIGUOUS" for r in rows),
        reason_counts=dict(sorted(Counter(p.reason for p in predicates).items())),
        predicate_qc_status_counts=dict(sorted(Counter(p.qc_status for p in predicates).items())),
    )


class NumericEvaluationResult(Declaration):
    schema_version: Literal["discovery-numeric-evaluation-v1"] = "discovery-numeric-evaluation-v1"
    semantics_version: Literal["EXPLORATORY_NUMERIC_ISSUE7_V1"] = "EXPLORATORY_NUMERIC_ISSUE7_V1"
    data_classification: Literal["LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"] = "LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"
    scientific_validation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    scientific_status: Literal["NOT_VALIDATED"] = "NOT_VALIDATED"
    scientific_hit_status: Literal["NOT_CONFIRMED"] = "NOT_CONFIRMED"
    warning: Literal["SCIENTIFICALLY_UNVERIFIED"] = "SCIENTIFICALLY_UNVERIFIED"
    verification_basis: Literal["FROZEN_AUDIT_STRUCTURE_AND_NUMERIC_REPLAY"] = "FROZEN_AUDIT_STRUCTURE_AND_NUMERIC_REPLAY"
    measurement_audit: MeasurementAuditResult
    condition: NumericCondition
    context: NumericContext | None
    rows: tuple[NumericRowResult, ...]
    counts: NumericCounts
    evaluated_at: datetime
    evaluation_id: Text

    @field_validator("evaluated_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("NUMERIC_TIMEZONE_REQUIRED")
        return value

    def identity_payload(self):
        payload = self.model_dump(mode="json", exclude={"evaluation_id", "evaluated_at"})
        payload["measurement_audit"].pop("audited_at")
        payload["measurement_audit"]["identifiers"]["inspection"]["manifest"].pop("inspected_at")
        return payload

    @model_validator(mode="after")
    def replay_internal_structure(self):
        audit = _deep_validate(MeasurementAuditResult, self.measurement_audit)
        condition = _deep_validate(NumericCondition, self.condition)
        context = _deep_validate(NumericContext, self.context) if self.context is not None else None
        rows = tuple(_deep_validate(NumericRowResult, r) for r in self.rows)
        _validate_inputs(audit, condition, context)
        if rows != _evaluate_rows(audit, condition, context):
            raise ValueError("NUMERIC_RESULT_REPLAY_MISMATCH")
        if self.counts != _counts(rows):
            raise ValueError("NUMERIC_COUNTS_MISMATCH")
        if self.evaluation_id != "NUMERIC_" + DatasetManifest.content_digest(self.identity_payload()):
            raise ValueError("NUMERIC_RESULT_ID_MISMATCH")
        return self

    @property
    def exploratory_matches(self):
        return tuple(r for r in self.rows if r.truth == "T")

    @property
    def qc_flagged_matches(self):
        return tuple(r for r in self.rows if r.truth == "T" and r.has_declared_qc_failure)

    @property
    def nonmatches(self):
        return tuple(r for r in self.rows if r.truth == "F")

    @property
    def unknowns(self):
        return tuple(r for r in self.rows if r.truth == "U")

    def safe_summary(self):
        """No paths/identifiers/thresholds/metadata. Real aggregates remain private."""
        return dict(schema_version=self.schema_version, data_classification=self.data_classification,
                    scientific_validation=self.scientific_validation, scientific_status=self.scientific_status,
                    scientific_hit_status=self.scientific_hit_status, warning=self.warning,
                    **self.counts.model_dump(mode="json"))


def _load(text, model, code):
    try:
        return _deep_validate(model, _declaration_json(text))
    except (ValueError, TypeError, RecursionError):
        raise NumericEvaluationError(code) from None


def load_numeric_condition(text):
    return _load(text, NumericCondition, "INVALID_NUMERIC_CONDITION")


def load_numeric_context(text):
    return _load(text, NumericContext, "INVALID_NUMERIC_CONTEXT")


def load_numeric_evaluation(text):
    """Embedded input/parse/compare replay; no fresh source or external authentication."""
    return _load(text, NumericEvaluationResult, "INVALID_NUMERIC_EVALUATION")


def evaluate_numeric_conditions(measurement_audit, condition, *, context=None,
                                max_evaluations=DEFAULT_MAX_EVALUATIONS):
    if type(max_evaluations) is not int or max_evaluations < 1:
        raise NumericEvaluationError("INVALID_NUMERIC_EVALUATION_LIMIT")
    try:
        audit = _deep_validate(MeasurementAuditResult, measurement_audit)
    except (ValueError, TypeError, RecursionError):
        raise NumericEvaluationError("INVALID_MEASUREMENT_AUDIT") from None
    try:
        condition = _deep_validate(NumericCondition, condition)
    except (ValueError, TypeError, RecursionError):
        raise NumericEvaluationError("INVALID_NUMERIC_CONDITION") from None
    try:
        context = _deep_validate(NumericContext, context) if context is not None else None
    except (ValueError, TypeError, RecursionError):
        raise NumericEvaluationError("INVALID_NUMERIC_CONTEXT") from None
    _validate_inputs(audit, condition, context)
    if len(audit.rows) * len(condition.predicates) > max_evaluations:
        raise NumericEvaluationError("NUMERIC_EVALUATION_LIMIT")
    rows = _evaluate_rows(audit, condition, context)
    payload = dict(measurement_audit=audit, condition=condition, context=context, rows=rows,
                   counts=_counts(rows), evaluated_at=datetime.now(timezone.utc))
    provisional = NumericEvaluationResult.model_construct(**payload, evaluation_id="pending")
    payload["evaluation_id"] = "NUMERIC_" + DatasetManifest.content_digest(provisional.identity_payload())
    return NumericEvaluationResult(**payload)


def replay_numeric_evaluation(result, *, measurement_audit, condition, context=None,
                              mapping_reference=None, max_evaluations=DEFAULT_MAX_EVALUATIONS):
    """Match independent frozen inputs/references and replay. Does not open a table."""
    try:
        prior = _deep_validate(NumericEvaluationResult, result)
    except (ValueError, TypeError, RecursionError):
        raise NumericEvaluationError("INVALID_NUMERIC_EVALUATION") from None
    replay = evaluate_numeric_conditions(measurement_audit, condition, context=context, max_evaluations=max_evaluations)
    if replay.evaluation_id != prior.evaluation_id:
        raise NumericEvaluationError("NUMERIC_REFERENCE_REPLAY_MISMATCH")
    identifiers = replay.measurement_audit.identifiers
    if identifiers.reference_supplied != (mapping_reference is not None):
        raise NumericEvaluationError("NUMERIC_MAPPING_REFERENCE_REQUIRED")
    if mapping_reference is not None:
        try:
            reference = _deep_validate(LocalMappingReference, mapping_reference)
            if reference.binding != identifiers.column_contract.mapping_reference:
                raise ValueError("NUMERIC_MAPPING_BINDING_MISMATCH")
            _replay_mapping(identifiers, reference)
        except (ValueError, TypeError, RecursionError):
            raise NumericEvaluationError("NUMERIC_MAPPING_REPLAY_FAILED") from None
    return prior


def verify_numeric_source(path, result, *, mapping_reference=None, max_evaluations=DEFAULT_MAX_EVALUATIONS,
                          max_bytes=DEFAULT_MAX_BYTES, max_rows=DEFAULT_MAX_ROWS, max_cells=DEFAULT_MAX_CELLS):
    """Verify current source under frozen declarations; no scientific certification."""
    try:
        prior = _deep_validate(NumericEvaluationResult, result)
    except (ValueError, TypeError, RecursionError):
        raise NumericEvaluationError("INVALID_NUMERIC_EVALUATION") from None
    fresh = verify_measurement_source(path, prior.measurement_audit, mapping_reference=mapping_reference,
                                      max_bytes=max_bytes, max_rows=max_rows, max_cells=max_cells)
    replay = evaluate_numeric_conditions(fresh, prior.condition, context=prior.context, max_evaluations=max_evaluations)
    if replay.evaluation_id != prior.evaluation_id:
        raise NumericEvaluationError("NUMERIC_SOURCE_REPLAY_MISMATCH")
    return replay
