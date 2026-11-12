"""Local, three-valued membership evaluation with no provider execution."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator

from .category_contracts import (
    AnnotationBinding, AnnotationReference, CategoryAssertion, CategoryCondition,
    CategoryDeclaration,
)
from .contracts import ColumnContract, Digest, LexicalPolicy, LocalMappingReference, MappingBinding, Text
from .identifiers import (
    Count, IdentifierInspection, IdentifierRecord, Status, _declaration_json,
    inspect_identifiers, load_column_contract, load_mapping_reference,
)
from .models import DatasetManifest
from .tables import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, DatasetInspectionError

Truth = Literal["T", "F", "U"]
Outcome = Literal["INCLUDED", "EXCLUDED", "UNKNOWN"]
Reason = Literal[
    "ENTITY_RECORD_MISSING", "CATEGORY_ASSERTION_MISSING", "OUTSIDE_CATEGORY_COVERAGE",
    "COMPLETE_SCOPE_REQUIRED", "SOURCE_SCOPE_MISMATCH", "SOURCE_RELEASE_SCOPE_MISMATCH",
    "OUTSIDE_ASSERTION_SCOPE", "SCOPE_NOT_COMPLETE", "UNCERTAIN_ASSERTION",
    "CONFLICT", "AMBIGUOUS_ID_RETAINED", "NO_APPROVED_ASSOCIATION",
]
ValidationLevel = Literal["FRESH_SNAPSHOT_PROCESSING", "SERIALIZED_INPUT_REFERENCE_REPLAY"]


class CategoryEvaluationError(DatasetInspectionError):
    def __init__(self, code: str):
        super().__init__(code, "local category declaration or evaluation failed")


def _load(text, model, code):
    try:
        return model.model_validate(_declaration_json(text))
    except (ValueError, TypeError, RecursionError):
        raise CategoryEvaluationError(code) from None


def load_annotation_reference(text: str | bytes) -> AnnotationReference:
    return _load(text, AnnotationReference, "INVALID_ANNOTATION_REFERENCE")


def load_category_condition(text: str | bytes) -> CategoryCondition:
    return _load(text, CategoryCondition, "INVALID_CATEGORY_CONDITION")


def _combine(values, operator):
    if operator == "ANY":
        return "T" if "T" in values else ("U" if "U" in values else "F")
    return "F" if "F" in values else ("U" if "U" in values else "T")


def _ordered_reasons(values):
    return tuple(dict.fromkeys(values))


class AssertionEvidence(CategoryDeclaration):
    entity_record_id: Text
    assertion: CategoryAssertion
    applicability: Literal["DETERMINATE", "UNDETERMINED", "INAPPLICABLE"]
    reasons: tuple[Reason, ...]


class PredicateEvaluation(CategoryDeclaration):
    condition_id: Text
    category_id: Text
    entity_record_status: Literal["PRESENT", "ENTITY_RECORD_MISSING"]
    coverage_status: Literal["INSIDE_DECLARED_SCOPE", "OUTSIDE_CATEGORY_COVERAGE"]
    covering_scope_ids: tuple[Text, ...]
    category_state: Literal["PRESENT", "ABSENT", "UNANNOTATED", "UNCERTAIN", "CONFLICTING"]
    truth: Truth
    evidence: tuple[AssertionEvidence, ...]
    reasons: tuple[Reason, ...]


class TargetEvaluation(CategoryDeclaration):
    identifier: Text
    predicates: tuple[PredicateEvaluation, ...]
    truth: Truth
    decisive_condition_ids: tuple[Text, ...]


class RowEvaluation(CategoryDeclaration):
    identifier_record: IdentifierRecord
    association_status: Literal["DECLARED_RAW_KEY", "REFERENCE_REPLAYED_TARGETS", "NO_APPROVED_ASSOCIATION"]
    targets: tuple[TargetEvaluation, ...]
    truth: Truth
    outcome: Outcome
    reasons: tuple[Reason, ...]


class EvaluationInputBinding(CategoryDeclaration):
    dataset_id: Text
    source_file_sha256: Digest
    source_file_size: Count
    inspection_id: Text
    processing_id: Text
    column_contract_sha256: Digest
    mapping_binding: MappingBinding | None
    namespace: Text
    taxon: Text
    lexical_policy: LexicalPolicy


def _input_binding(identifiers):
    manifest = identifiers.inspection.manifest
    contract = identifiers.column_contract
    return EvaluationInputBinding(
        dataset_id=manifest.dataset_id, source_file_sha256=manifest.source_file_sha256,
        source_file_size=manifest.source_file_size, inspection_id=manifest.inspection_id,
        processing_id=identifiers.processing_id, column_contract_sha256=identifiers.contract_sha256,
        mapping_binding=contract.mapping_reference, namespace=contract.namespace,
        taxon=contract.taxon, lexical_policy=contract.lexical_policy,
    )


class EvaluationCounts(CategoryDeclaration):
    input_row_count: Count
    row_outcomes: dict[Outcome, Count]
    identity_status_counts: dict[Status, Count]
    association_mode: Literal["DECLARED_DIRECT_KEY", "BOUND_MAPPING_TARGET"]
    associated_key_occurrence_count: Count
    predicate_occurrence_count: Count
    target_truth_counts: dict[Truth, Count]
    predicate_truth_counts: dict[Truth, Count]
    ambiguous_row_count: Count
    raw_duplicate_row_count: Count
    lexical_duplicate_row_count: Count
    conflict_row_count: Count
    blank_record_count: Count
    reason_row_counts: dict[Reason, Count]
    reason_counts_overlap: Literal[True] = True


def _counts(identifiers, rows, conditions):
    targets = [t for r in rows for t in r.targets]
    predicates = [p for t in targets for p in t.predicates]
    return EvaluationCounts(
        input_row_count=len(rows), row_outcomes={o: sum(r.outcome == o for r in rows) for o in ("INCLUDED", "EXCLUDED", "UNKNOWN")},
        identity_status_counts=dict(sorted(Counter(r.identifier_record.status for r in rows).items())),
        association_mode=conditions.association_mode, associated_key_occurrence_count=len(targets),
        predicate_occurrence_count=len(predicates),
        target_truth_counts={v: sum(t.truth == v for t in targets) for v in ("T", "F", "U")},
        predicate_truth_counts={v: sum(p.truth == v for p in predicates) for v in ("T", "F", "U")},
        ambiguous_row_count=sum(r.identifier_record.status == "UNRESOLVED_AMBIGUOUS" for r in rows),
        raw_duplicate_row_count=sum(r.identifier_record.raw_occurrence_count > 1 for r in rows),
        lexical_duplicate_row_count=sum(r.identifier_record.lexical_occurrence_count > 1 for r in rows),
        conflict_row_count=sum("CONFLICT" in r.reasons for r in rows),
        blank_record_count=identifiers.inspection.manifest.blank_record_count,
        reason_row_counts=dict(sorted(Counter(reason for r in rows for reason in r.reasons).items())),
    )


def _check_configuration(identifiers, annotation, conditions, reference):
    if annotation.binding != conditions.annotation_binding:
        raise CategoryEvaluationError("ANNOTATION_BINDING_MISMATCH")
    if not {p.category_id for p in conditions.predicates} <= {v.category_id for v in annotation.vocabulary}:
        raise CategoryEvaluationError("CATEGORY_NOT_DECLARED")
    contract = identifiers.column_contract
    binding = contract.mapping_reference
    if conditions.association_mode == "DECLARED_DIRECT_KEY":
        if binding is not None or reference is not None:
            raise CategoryEvaluationError("DIRECT_MAPPING_CONFLICT")
        namespace = contract.namespace
    else:
        if binding is None:
            raise CategoryEvaluationError("MAPPING_BINDING_REQUIRED")
        if reference is None:
            raise CategoryEvaluationError("MAPPING_REFERENCE_REQUIRED")
        if reference.binding != binding:
            raise CategoryEvaluationError("MAPPING_REFERENCE_BINDING_MISMATCH")
        namespace = binding.target_namespace
    if annotation.namespace != namespace or annotation.taxon != contract.taxon:
        raise CategoryEvaluationError("ANNOTATION_NAMESPACE_TAXON_MISMATCH")


def _association_keys(record, mode):
    if record.status in ("BLANK", "DECLARED_MISSING", "EMPTY_AFTER_POLICY"):
        return ()
    if mode == "DECLARED_DIRECT_KEY":
        return (record.raw_identifier,)
    return record.mapping_targets


def _replay_mapping(identifiers, reference):
    index = defaultdict(list)
    for entry in reference.entries:
        index[entry.source].append(entry.target)
    if not identifiers.reference_supplied:
        raise CategoryEvaluationError("IDENTIFIER_REFERENCE_REPLAY_MISMATCH")
    for record in identifiers.identifiers:
        hits = index.get(record.lexical_identifier, []) if record.lexical_identifier is not None else []
        if record.mapping_targets != tuple(sorted(set(hits))) or record.mapping_entry_count != len(hits):
            raise CategoryEvaluationError("IDENTIFIER_REFERENCE_REPLAY_MISMATCH")


def _prepare_scopes(annotation):
    # No target × category expansion. Exact membership sets are cached once,
    # while dictionary insertion order preserves the reference's scope order.
    return {s.scope_id: (s, frozenset(s.covered_identifiers), frozenset(s.category_ids))
            for s in annotation.complete_scopes}


def _entity_index(annotation):
    entities = defaultdict(list)
    for entity in annotation.entities:
        entities[entity.target_identifier].append(entity)
    return entities


def _evidence(entity, assertion, target, scopes):
    reasons = []
    prepared = scopes.get(assertion.complete_scope_id)
    scope = prepared[0] if prepared else None
    applicability = "DETERMINATE"
    if scope is not None:
        if scope.source_binding.source_id != assertion.source_binding.source_id:
            reasons.append("SOURCE_SCOPE_MISMATCH")
        elif scope.source_binding.release != assertion.source_binding.release:
            reasons.append("SOURCE_RELEASE_SCOPE_MISMATCH")
        if target not in prepared[1] or assertion.category_id not in prepared[2]:
            reasons.append("OUTSIDE_ASSERTION_SCOPE")
        if reasons:
            applicability = "INAPPLICABLE"
    if applicability != "INAPPLICABLE":
        if assertion.state == "ABSENT":
            if scope is None:
                applicability = "UNDETERMINED"
                reasons.append("COMPLETE_SCOPE_REQUIRED")
            elif scope.coverage_status != "COMPLETE":
                applicability = "UNDETERMINED"
                reasons.append("SCOPE_NOT_COMPLETE")
        elif assertion.state == "UNCERTAIN":
            applicability = "UNDETERMINED"
            reasons.append("UNCERTAIN_ASSERTION")
    return AssertionEvidence(entity_record_id=entity.entity_record_id, assertion=assertion,
                             applicability=applicability, reasons=tuple(reasons))


def _evaluate_predicate(target, predicate, entities, scopes):
    matching_entities = entities.get(target, ())
    covering = tuple(s.scope_id for s, targets, category_ids in scopes.values()
                     if target in targets and predicate.category_id in category_ids)
    evidence = tuple(_evidence(e, a, target, scopes) for e in matching_entities
                     for a in e.assertions if a.category_id == predicate.category_id)
    reasons = []
    if not matching_entities:
        reasons.append("ENTITY_RECORD_MISSING")
    if not evidence:
        reasons.append("CATEGORY_ASSERTION_MISSING")
    if not covering:
        reasons.append("OUTSIDE_CATEGORY_COVERAGE")
    reasons.extend(reason for e in evidence for reason in e.reasons)
    state, truth, conflict = _category_decision(evidence)
    if conflict:
        reasons.append("CONFLICT")
    return PredicateEvaluation(
        condition_id=predicate.condition_id, category_id=predicate.category_id,
        entity_record_status="PRESENT" if matching_entities else "ENTITY_RECORD_MISSING",
        coverage_status="INSIDE_DECLARED_SCOPE" if covering else "OUTSIDE_CATEGORY_COVERAGE",
        covering_scope_ids=covering, category_state=state, truth=truth,
        evidence=evidence, reasons=_ordered_reasons(reasons),
    )


def _category_decision(evidence):
    """Internal truth consistency given the recorded evidence applicability."""
    positive = any(e.applicability == "DETERMINATE" and e.assertion.state == "PRESENT" for e in evidence)
    negative = any(e.applicability == "DETERMINATE" and e.assertion.state == "ABSENT" for e in evidence)
    uncertain = any(e.applicability == "UNDETERMINED" for e in evidence)
    if positive and negative:
        state, truth = "CONFLICTING", "U"
    elif uncertain:
        state, truth = "UNCERTAIN", "U"
    elif positive:
        state, truth = "PRESENT", "T"
    elif negative:
        state, truth = "ABSENT", "F"
    else:
        state, truth = ("UNCERTAIN" if evidence else "UNANNOTATED"), "U"
    return state, truth, positive and negative


def _evaluate_rows(identifiers, annotation, conditions):
    entities = _entity_index(annotation)
    scopes = _prepare_scopes(annotation)
    rows = []
    for record in identifiers.identifiers:
        targets = []
        for key in _association_keys(record, conditions.association_mode):
            predicates = tuple(_evaluate_predicate(key, p, entities, scopes) for p in conditions.predicates)
            truth = _combine([p.truth for p in predicates], conditions.combine)
            decisive = tuple(p.condition_id for p in predicates if p.truth == truth) if truth != "U" else ()
            targets.append(TargetEvaluation(identifier=key, predicates=predicates, truth=truth,
                                            decisive_condition_ids=decisive))
        reasons = [reason for t in targets for p in t.predicates for reason in p.reasons]
        if record.status == "UNRESOLVED_AMBIGUOUS":
            truth = "U"
            reasons.append("AMBIGUOUS_ID_RETAINED")
        elif not targets:
            truth = "U"
            reasons.append("NO_APPROVED_ASSOCIATION")
        else:
            truth = targets[0].truth
        rows.append(RowEvaluation(
            identifier_record=record, targets=tuple(targets), truth=truth,
            outcome={"T": "INCLUDED", "F": "EXCLUDED", "U": "UNKNOWN"}[truth],
            association_status=("NO_APPROVED_ASSOCIATION" if not targets else
                                "DECLARED_RAW_KEY" if conditions.association_mode == "DECLARED_DIRECT_KEY" else "REFERENCE_REPLAYED_TARGETS"),
            reasons=_ordered_reasons(reasons),
        ))
    return tuple(rows)


class EvaluationResult(CategoryDeclaration):
    schema_version: Literal["discovery-category-evaluation-v1"] = "discovery-category-evaluation-v1"
    semantics_version: Literal["category-membership-three-valued-v1"] = "category-membership-three-valued-v1"
    data_classification: Literal["LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"] = "LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"
    identity_verification: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    declaration_basis: Literal["USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"] = "USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"
    negative_policy: Literal["EXPLICIT_ABSENT_AND_SOURCE_MATCHED_COMPLETE_SCOPE"] = "EXPLICIT_ABSENT_AND_SOURCE_MATCHED_COMPLETE_SCOPE"
    validation_level: ValidationLevel
    input_binding: EvaluationInputBinding
    identifiers: IdentifierInspection
    annotation_binding: AnnotationBinding
    annotation_reference: AnnotationReference
    condition_binding: Digest
    conditions: CategoryCondition
    rows: tuple[RowEvaluation, ...]
    counts: EvaluationCounts
    evaluated_at: datetime
    evaluation_id: Text

    @field_validator("evaluated_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("CATEGORY_TIMEZONE_REQUIRED")
        return value

    def identity_payload(self):
        payload = self.model_dump(mode="json", exclude={"evaluation_id", "evaluated_at"})
        # Retain the entire frozen input/contracts, omit only operational time.
        payload["identifiers"]["inspection"]["manifest"].pop("inspected_at")
        return payload

    @model_validator(mode="after")
    def structural_consistency(self):
        # These are internal structural checks, not external reference/source
        # verification. Replay uses separately supplied, digest-matched inputs.
        if self.input_binding != _input_binding(self.identifiers):
            raise ValueError("CATEGORY_INPUT_BINDING_CONFLICT")
        if self.annotation_binding != self.annotation_reference.binding or self.conditions.annotation_binding != self.annotation_binding:
            raise ValueError("CATEGORY_ANNOTATION_BINDING_CONFLICT")
        if self.condition_binding != self.conditions.content_sha256:
            raise ValueError("CATEGORY_CONDITION_BINDING_CONFLICT")
        if tuple(r.identifier_record for r in self.rows) != self.identifiers.identifiers:
            raise ValueError("CATEGORY_ROW_LOCATOR_CONFLICT")
        contract = self.identifiers.column_contract
        mode = self.conditions.association_mode
        namespace = contract.namespace
        if mode == "BOUND_MAPPING_TARGET":
            if contract.mapping_reference is None or not self.identifiers.reference_supplied:
                raise ValueError("CATEGORY_MAPPING_BINDING_REQUIRED")
            namespace = contract.mapping_reference.target_namespace
        elif contract.mapping_reference is not None:
            raise ValueError("CATEGORY_DIRECT_MAPPING_CONFLICT")
        if namespace != self.annotation_binding.namespace or contract.taxon != self.annotation_binding.taxon:
            raise ValueError("CATEGORY_NAMESPACE_TAXON_CONFLICT")
        categories = {c.category_id for c in self.annotation_reference.vocabulary}
        if not {p.category_id for p in self.conditions.predicates} <= categories:
            raise ValueError("CATEGORY_CONDITION_CATEGORY_MISSING")
        original_assertions = {(e.entity_record_id, a.assertion_id): (e.target_identifier, a)
                               for e in self.annotation_reference.entities for a in e.assertions}
        scopes = _prepare_scopes(self.annotation_reference)
        entity_index = _entity_index(self.annotation_reference)
        for row in self.rows:
            if tuple(t.identifier for t in row.targets) != _association_keys(row.identifier_record, mode):
                raise ValueError("CATEGORY_TARGET_LINK_CONFLICT")
            expected_status = ("NO_APPROVED_ASSOCIATION" if not row.targets else
                               "DECLARED_RAW_KEY" if mode == "DECLARED_DIRECT_KEY" else "REFERENCE_REPLAYED_TARGETS")
            if row.association_status != expected_status:
                raise ValueError("CATEGORY_ASSOCIATION_CONFLICT")
            for target in row.targets:
                if tuple((p.condition_id, p.category_id) for p in target.predicates) != tuple((p.condition_id, p.category_id) for p in self.conditions.predicates):
                    raise ValueError("CATEGORY_PREDICATE_LINK_CONFLICT")
                for predicate in target.predicates:
                    covering = tuple(s.scope_id for s, targets, category_ids in scopes.values() if target.identifier in targets and predicate.category_id in category_ids)
                    if predicate.covering_scope_ids != covering or predicate.coverage_status != ("INSIDE_DECLARED_SCOPE" if covering else "OUTSIDE_CATEGORY_COVERAGE"):
                        raise ValueError("CATEGORY_SCOPE_LINK_CONFLICT")
                    entities = entity_index.get(target.identifier, ())
                    expected_assertions = tuple((e.entity_record_id, a) for e in entities for a in e.assertions if a.category_id == predicate.category_id)
                    if tuple((e.entity_record_id, e.assertion) for e in predicate.evidence) != expected_assertions:
                        raise ValueError("CATEGORY_EVIDENCE_RETENTION_CONFLICT")
                    if predicate.entity_record_status != ("PRESENT" if entities else "ENTITY_RECORD_MISSING"):
                        raise ValueError("CATEGORY_ENTITY_STATE_CONFLICT")
                    for evidence in predicate.evidence:
                        original = original_assertions.get((evidence.entity_record_id, evidence.assertion.assertion_id))
                        if original != (target.identifier, evidence.assertion) or evidence.assertion.category_id != predicate.category_id:
                            raise ValueError("CATEGORY_EVIDENCE_LINK_CONFLICT")
                    state, truth, conflict = _category_decision(predicate.evidence)
                    if (predicate.category_state, predicate.truth) != (state, truth):
                        raise ValueError("CATEGORY_STATE_TRUTH_CONFLICT")
                    reasons = []
                    if not entities:
                        reasons.append("ENTITY_RECORD_MISSING")
                    if not predicate.evidence:
                        reasons.append("CATEGORY_ASSERTION_MISSING")
                    if not covering:
                        reasons.append("OUTSIDE_CATEGORY_COVERAGE")
                    reasons.extend(reason for e in predicate.evidence for reason in e.reasons)
                    if conflict:
                        reasons.append("CONFLICT")
                    if predicate.reasons != _ordered_reasons(reasons):
                        raise ValueError("CATEGORY_REASON_CONFLICT")
                truth = _combine([p.truth for p in target.predicates], self.conditions.combine)
                decisive = tuple(p.condition_id for p in target.predicates if p.truth == truth) if truth != "U" else ()
                if target.truth != truth or target.decisive_condition_ids != decisive:
                    raise ValueError("CATEGORY_TARGET_TRUTH_CONFLICT")
            reasons = [reason for t in row.targets for p in t.predicates for reason in p.reasons]
            if row.identifier_record.status == "UNRESOLVED_AMBIGUOUS":
                truth = "U"
                reasons.append("AMBIGUOUS_ID_RETAINED")
            elif not row.targets:
                truth = "U"
                reasons.append("NO_APPROVED_ASSOCIATION")
            else:
                truth = row.targets[0].truth
            if row.truth != truth or row.outcome != {"T": "INCLUDED", "F": "EXCLUDED", "U": "UNKNOWN"}[truth] or row.reasons != _ordered_reasons(reasons):
                raise ValueError("CATEGORY_ROW_TRUTH_CONFLICT")
        if self.counts != _counts(self.identifiers, self.rows, self.conditions):
            raise ValueError("CATEGORY_COUNTS_CONFLICT")
        if self.evaluation_id != "CATEGORIES_" + DatasetManifest.content_digest(self.identity_payload()):
            raise ValueError("CATEGORY_EVALUATION_ID_MISMATCH")
        return self

    @property
    def confirmed_matches(self):
        return tuple(r for r in self.rows if r.outcome == "INCLUDED")

    @property
    def nonmatches(self):
        return tuple(r for r in self.rows if r.outcome == "EXCLUDED")

    @property
    def unknowns(self):
        return tuple(r for r in self.rows if r.outcome == "UNKNOWN")

    @property
    def conflicts(self):
        return tuple(r for r in self.rows if "CONFLICT" in r.reasons)

    def safe_summary(self):
        """Fixed statuses/counts only. Real aggregates still must remain local."""
        return dict(schema_version=self.schema_version,
                    data_classification=self.data_classification,
                    scientific_validation="NOT_PERFORMED", identity_verification=self.identity_verification,
                    **self.counts.model_dump(mode="json"))


def load_evaluation_result(text: str | bytes) -> EvaluationResult:
    """Internal structure/digest only; does not confirm references or source bytes."""
    return _load(text, EvaluationResult, "INVALID_CATEGORY_EVALUATION")


def evaluate_categories(
    identifiers: IdentifierInspection | Mapping, *,
    annotation_reference: AnnotationReference | Mapping,
    conditions: CategoryCondition | Mapping,
    mapping_reference: LocalMappingReference | Mapping | None = None,
) -> EvaluationResult:
    """Reference replay on supplied 6B2 input; no source-file verification."""
    return _evaluate(identifiers, annotation_reference, conditions, mapping_reference,
                     "SERIALIZED_INPUT_REFERENCE_REPLAY")


def _deep_validate(model, payload):
    # A Mapping can embed legacy model instances just as a top-level model can.
    # The first pass checks the outer type/structure; rebuilding every field
    # forces the second pass to validate all nested legacy model declarations.
    # Suppress serialization warnings from unvalidated model_copy values: the
    # public boundary must report only its fixed error, never those values.
    validated = model.model_validate(payload)
    return model.model_validate(validated.model_dump(mode="python", warnings=False))


def _evaluate(identifiers, annotation_reference, conditions, mapping_reference, validation_level):
    try:
        identifiers = _deep_validate(IdentifierInspection, identifiers)
    except (ValueError, TypeError, RecursionError):
        raise CategoryEvaluationError("INVALID_IDENTIFIER_INSPECTION") from None
    try:
        annotation = AnnotationReference.model_validate(annotation_reference)
    except (ValueError, TypeError, RecursionError):
        raise CategoryEvaluationError("INVALID_ANNOTATION_REFERENCE") from None
    if conditions is None:
        raise CategoryEvaluationError("CATEGORY_CONDITIONS_REQUIRED")
    try:
        condition = CategoryCondition.model_validate(conditions)
    except (ValueError, TypeError, RecursionError):
        raise CategoryEvaluationError("INVALID_CATEGORY_CONDITION") from None
    try:
        reference = LocalMappingReference.model_validate(mapping_reference) if mapping_reference is not None else None
    except (ValueError, TypeError, RecursionError):
        raise CategoryEvaluationError("INVALID_MAPPING_REFERENCE") from None
    _check_configuration(identifiers, annotation, condition, reference)
    if reference is not None:
        _replay_mapping(identifiers, reference)
    rows = _evaluate_rows(identifiers, annotation, condition)
    payload = dict(validation_level=validation_level, input_binding=_input_binding(identifiers),
                   identifiers=identifiers, annotation_binding=annotation.binding,
                   annotation_reference=annotation, condition_binding=condition.content_sha256,
                   conditions=condition, rows=rows, counts=_counts(identifiers, rows, condition),
                   evaluated_at=datetime.now(timezone.utc))
    provisional = EvaluationResult.model_construct(**payload, evaluation_id="pending")
    payload["evaluation_id"] = "CATEGORIES_" + DatasetManifest.content_digest(provisional.identity_payload())
    return EvaluationResult(**payload)


def replay_evaluation_result(result, *, annotation_reference, conditions, mapping_reference=None):
    """Verify external frozen references/condition and replay; never reopen source."""
    try:
        result = _deep_validate(EvaluationResult, result)
    except (ValueError, TypeError, RecursionError):
        raise CategoryEvaluationError("INVALID_CATEGORY_EVALUATION") from None
    replayed = _evaluate(result.identifiers, annotation_reference, conditions, mapping_reference,
                         result.validation_level)
    if replayed.identity_payload() != result.identity_payload():
        raise CategoryEvaluationError("CATEGORY_REFERENCE_REPLAY_MISMATCH")
    return result


def _read_local_declaration(path: str | Path, limit: int):
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(limit + 1)
    except (OSError, ValueError, TypeError):
        raise CategoryEvaluationError("DECLARATION_UNAVAILABLE") from None
    if len(raw) > limit:
        raise CategoryEvaluationError("DECLARATION_BYTE_LIMIT")
    return raw


def inspect_and_evaluate_categories(
    table_path: str | Path, column_contract: ColumnContract | Mapping, *,
    annotation_reference: AnnotationReference | Mapping, conditions: CategoryCondition | Mapping,
    mapping_reference: LocalMappingReference | Mapping | None = None,
    input_format: str | None = None, encoding: str = "utf-8-sig", confirm_header: bool = False,
    inspected_at: datetime | None = None, max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> EvaluationResult:
    identifiers = inspect_identifiers(table_path, column_contract, mapping_reference=mapping_reference,
                                     input_format=input_format, encoding=encoding,
                                     confirm_header=confirm_header, inspected_at=inspected_at,
                                     max_bytes=max_bytes, max_rows=max_rows)
    return _evaluate(identifiers, annotation_reference, conditions, mapping_reference, "FRESH_SNAPSHOT_PROCESSING")


def evaluate_category_files(table_path, column_contract_path, *, annotation_reference_path,
                            condition_path, mapping_reference_path=None, **parser_options):
    """Read each explicit declaration once, bounded; table uses the 6B1 snapshot."""
    contract = load_column_contract(_read_local_declaration(column_contract_path, 1024 * 1024))
    mapping = (load_mapping_reference(_read_local_declaration(mapping_reference_path, 64 * 1024 * 1024))
               if mapping_reference_path is not None else None)
    annotation = load_annotation_reference(_read_local_declaration(annotation_reference_path, 64 * 1024 * 1024))
    condition = load_category_condition(_read_local_declaration(condition_path, 1024 * 1024))
    return inspect_and_evaluate_categories(table_path, contract, mapping_reference=mapping,
                                           annotation_reference=annotation, conditions=condition, **parser_options)
