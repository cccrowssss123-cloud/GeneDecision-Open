"""Entirely invented fixtures: no biological identifiers, sources or results."""

import copy
import hashlib
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from genedecision.discovery import (
    AnnotationReference, CategoryCondition, CategoryEvaluationError, ColumnContract,
    EvaluationResult, LocalMappingReference, MappingEntry, evaluate_categories,
    evaluate_category_files, inspect_and_evaluate_categories, inspect_identifiers,
    load_annotation_reference, load_category_condition, load_evaluation_result,
    replay_evaluation_result,
)
from genedecision.discovery import categories, tables
from genedecision.discovery.__main__ import main
from genedecision.discovery.models import DatasetManifest
from test_discovery_tables import no_runtime_dependencies  # shared synthetic dependency guard

pytestmark = pytest.mark.usefixtures("no_runtime_dependencies")
STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
S1 = dict(source_id="SYN_SOURCE_1", release="SYN_RELEASE_1")
S2 = dict(source_id="SYN_SOURCE_2", release="SYN_RELEASE_1")
S1_OTHER = dict(source_id="SYN_SOURCE_1", release="SYN_RELEASE_2")
SOURCES = ((S1, "SYN_RECORD_1"), (S2, "SYN_RECORD_2"), (S1_OTHER, "SYN_RECORD_3"))


def record_for(source):
    return next(record for binding, record in SOURCES if binding == source)


def assertion(state="PRESENT", category="SYN_CAT_A", *, source=S1, scope=None, aid="SYN_ASSERT_1"):
    return dict(assertion_id=aid, category_id=category, state=state, source_binding=source,
                source_record_ids=[record_for(source)], complete_scope_id=scope)


def entity(target="SYN_TARGET_A", assertions=(), eid="SYN_ENTITY_1"):
    return dict(entity_record_id=eid, target_identifier=target, source_record_ids=["SYN_RECORD_1"],
                assertions=list(assertions))


def scope(*, targets=("SYN_TARGET_A", "SYN_TARGET_B"), cats=("SYN_CAT_A", "SYN_CAT_B"),
          source=S1, status="COMPLETE", sid="SYN_SCOPE_1"):
    return dict(scope_id=sid, source_binding=source, covered_identifiers=list(targets), category_ids=list(cats),
                coverage_status=status, scope_definition="Invented exact software test universe",
                source_record_ids=[record_for(source)])


def annotation(entities=(), scopes=(), **updates):
    payload = dict(reference_id="SYN_REFERENCE", release="SYN_CONTAINER_RELEASE", namespace="synthetic-target",
                   taxon="synthetic-taxon",
                   provenance=dict(description="Entirely invented software fixture", source_versions=[
                       dict(**binding, description="Invented source") for binding, _ in SOURCES]),
                   license=dict(identifier="SYN_TEST_ONLY", notes="Invented license declaration",
                                local_use="UNKNOWN", redistribution="UNKNOWN"),
                   vocabulary=[dict(category_id=c, label="Invented category", definition="Invented membership",
                                    definition_source="SYN_RECORD_1") for c in ("SYN_CAT_A", "SYN_CAT_B")],
                   source_records=[dict(record_id=r, source_binding=b, source_locator="SYN_LOCATOR") for b, r in SOURCES],
                   entities=list(entities), complete_scopes=list(scopes))
    payload.update(updates)
    return AnnotationReference.from_declarations(**payload)


def condition(ref, *, mode="DECLARED_DIRECT_KEY", combine="ALL", cats=("SYN_CAT_A",), **updates):
    payload = dict(annotation_binding=ref.binding, association_mode=mode, combine=combine,
                   direct_key_policy="RAW_EXACT" if mode == "DECLARED_DIRECT_KEY" else None,
                   predicates=[dict(condition_id=f"SYN_COND_{i}", category_id=c) for i, c in enumerate(cats)])
    payload.update(updates)
    return CategoryCondition.from_declarations(**payload)


def contract(mapping=None, **updates):
    payload = dict(primary_gene=dict(column="synthetic_feature", definition="Invented identifier column"),
                   measurements=[dict(column="synthetic_value", definition="Invented measurement declaration")],
                   namespace="synthetic-target" if mapping is None else "synthetic-source", taxon="synthetic-taxon",
                   mapping_reference=mapping.binding if mapping else None)
    payload.update(updates)
    return ColumnContract(**payload)


def mapping(entries):
    return LocalMappingReference.from_entries(reference_id="SYN_MAPPING", source_namespace="synthetic-source",
                                             target_namespace="synthetic-target", taxon="synthetic-taxon",
                                             entries=tuple(MappingEntry(source=s, target=t) for s, t in entries))


def table(tmp_path, keys=("SYN_TARGET_A",), name="synthetic.csv"):
    path = tmp_path / name
    path.write_text("synthetic_feature,synthetic_value\n" + "".join(f'"{k}",{i}\n' for i, k in enumerate(keys)))
    return path


def evaluate(tmp_path, ref, *, keys=("SYN_TARGET_A",), mapping_ref=None, combine="ALL", cats=("SYN_CAT_A",), column=None):
    cond = condition(ref, mode="BOUND_MAPPING_TARGET" if mapping_ref else "DECLARED_DIRECT_KEY", combine=combine, cats=cats)
    return inspect_and_evaluate_categories(table(tmp_path, keys), column or contract(mapping_ref),
                                           mapping_reference=mapping_ref, annotation_reference=ref, conditions=cond,
                                           inspected_at=STAMP)


def predicate(result, row=0, target=0, index=0):
    return result.rows[row].targets[target].predicates[index]


def test_sparse_missingness_preserves_rows_and_separate_diagnostics(tmp_path):
    ref = annotation([entity("SYN_TARGET_B")], [scope(targets=("SYN_TARGET_A", "SYN_TARGET_B"))])
    result = evaluate(tmp_path, ref, keys=("SYN_TARGET_A", "SYN_TARGET_B", "SYN_TARGET_C", "SYN_TARGET_A", "", "SYN_MISSING"),
                      column=contract(missing_identifier_tokens=["SYN_MISSING"]))
    assert result.counts.row_outcomes == dict(INCLUDED=0, EXCLUDED=0, UNKNOWN=6)
    assert result.confirmed_matches == result.nonmatches == result.conflicts == ()
    assert len(result.unknowns) == 6
    assert predicate(result).entity_record_status == "ENTITY_RECORD_MISSING"
    assert predicate(result).coverage_status == "INSIDE_DECLARED_SCOPE"
    assert predicate(result, row=1).entity_record_status == "PRESENT"
    assert predicate(result, row=1).reasons == ("CATEGORY_ASSERTION_MISSING",)
    assert predicate(result, row=2).reasons == ("ENTITY_RECORD_MISSING", "CATEGORY_ASSERTION_MISSING", "OUTSIDE_CATEGORY_COVERAGE")
    assert [r.identifier_record.locator.logical_record for r in result.rows] == list(range(1, 7))
    assert result.counts.raw_duplicate_row_count == 2
    assert result.counts.input_row_count == 6 and result.counts.associated_key_occurrence_count == 4
    assert result.rows[4].targets == result.rows[5].targets == ()


@pytest.mark.parametrize("state,scope_status,with_scope,truth", [
    ("PRESENT", "COMPLETE", False, "T"), ("ABSENT", "COMPLETE", True, "F"),
    ("ABSENT", "COMPLETE", False, "U"), ("ABSENT", "PARTIAL", True, "U"),
    ("ABSENT", "UNKNOWN", True, "U"), ("UNCERTAIN", "COMPLETE", False, "U"),
])
def test_explicit_absence_requires_applicable_complete_scope(tmp_path, state, scope_status, with_scope, truth):
    ref = annotation([entity(assertions=[assertion(state, scope="SYN_SCOPE_1" if with_scope else None)])],
                     [scope(status=scope_status)] if with_scope else [])
    result = evaluate(tmp_path, ref)
    assert result.rows[0].truth == truth
    assert len(predicate(result).evidence) == 1


@pytest.mark.parametrize("source,targets,cats,reason", [
    (S2, ("SYN_TARGET_A",), ("SYN_CAT_A",), "SOURCE_SCOPE_MISMATCH"),
    (S1_OTHER, ("SYN_TARGET_A",), ("SYN_CAT_A",), "SOURCE_RELEASE_SCOPE_MISMATCH"),
    (S1, ("SYN_TARGET_B",), ("SYN_CAT_A",), "OUTSIDE_ASSERTION_SCOPE"),
    (S1, ("SYN_TARGET_A",), ("SYN_CAT_B",), "OUTSIDE_ASSERTION_SCOPE"),
])
@pytest.mark.parametrize("positive", [False, True])
def test_inapplicable_negative_retained_without_false_or_fake_conflict(tmp_path, source, targets, cats, reason, positive):
    assertions = [assertion("ABSENT", source=source, scope="SYN_SCOPE_1")]
    if positive:
        assertions.append(assertion(aid="SYN_ASSERT_2"))
    ref = annotation([entity(assertions=assertions)], [scope(targets=targets, cats=cats)])
    result = evaluate(tmp_path, ref)
    assert result.rows[0].truth == ("T" if positive else "U")
    assessment = predicate(result)
    assert reason in assessment.evidence[0].reasons
    assert assessment.evidence[0].applicability == "INAPPLICABLE"
    assert "CONFLICT" not in assessment.reasons


@pytest.mark.parametrize("extra_state,extra_scope,truth,state", [
    ("PRESENT", None, "T", "PRESENT"), ("UNCERTAIN", None, "U", "UNCERTAIN"),
    ("ABSENT", None, "U", "UNCERTAIN"), ("ABSENT", "SYN_SCOPE_1", "U", "CONFLICTING"),
])
def test_repeated_and_uncertain_evidence_and_conflicts_never_vote(tmp_path, extra_state, extra_scope, truth, state):
    assertions = [assertion(aid=f"SYN_POS_{i}") for i in range(4)]
    assertions.append(assertion(extra_state, scope=extra_scope, aid="SYN_EXTRA"))
    ref = annotation([entity(assertions=assertions)], [scope()])
    result = evaluate(tmp_path, ref)
    assert predicate(result).truth == truth and predicate(result).category_state == state
    assert len(predicate(result).evidence) == 5
    assert len(result.conflicts) == int(state == "CONFLICTING")


def test_competing_sources_with_own_scopes_conflict(tmp_path):
    ref = annotation([entity(assertions=[assertion(), assertion("ABSENT", source=S2, scope="SYN_SCOPE_2", aid="SYN_NEG")])],
                     [scope(source=S2, sid="SYN_SCOPE_2")])
    assert evaluate(tmp_path, ref).counts.conflict_row_count == 1


@pytest.mark.parametrize("a,b", list(itertools.product("TFU", repeat=2)))
@pytest.mark.parametrize("combine", ["ANY", "ALL"])
def test_full_three_valued_truth_table(tmp_path, a, b, combine):
    states = {"T": "PRESENT", "F": "ABSENT", "U": "UNCERTAIN"}
    ref = annotation([entity(assertions=[assertion(states[a], scope="SYN_SCOPE_1", aid="SYN_A"),
                                        assertion(states[b], "SYN_CAT_B", scope="SYN_SCOPE_1", aid="SYN_B")])], [scope()])
    result = evaluate(tmp_path, ref, combine=combine, cats=("SYN_CAT_A", "SYN_CAT_B"))
    expected = ("T" if "T" in (a, b) else "U" if "U" in (a, b) else "F") if combine == "ANY" else (
        "F" if "F" in (a, b) else "U" if "U" in (a, b) else "T")
    assert result.rows[0].truth == expected
    assert [p.truth for p in result.rows[0].targets[0].predicates] == [a, b]
    assert result.counts.predicate_occurrence_count == 2


@pytest.mark.parametrize("states", [("T", "T"), ("T", "F"), ("F", "F"), ("T", "U"), ("U", "U")])
def test_all_one_to_many_targets_preserved_and_row_always_unknown(tmp_path, states):
    entities = []
    for i, state in enumerate(states):
        if state != "U":
            entities.append(entity(f"SYN_TARGET_{i}", [assertion("PRESENT" if state == "T" else "ABSENT", scope="SYN_SCOPE_1", aid=f"SYN_ASSERT_{i}")], f"SYN_ENTITY_{i}"))
    ref = annotation(entities, [scope(targets=("SYN_TARGET_0", "SYN_TARGET_1"))])
    m = mapping([("SYN_ALIAS", "SYN_TARGET_1"), ("SYN_ALIAS", "SYN_TARGET_0"), ("SYN_ALIAS", "SYN_TARGET_0")])
    result = evaluate(tmp_path, ref, keys=("SYN_ALIAS", "SYN_ALIAS"), mapping_ref=m)
    assert all(r.truth == "U" and r.outcome == "UNKNOWN" for r in result.rows)
    assert [t.truth for t in result.rows[0].targets] == list(states)
    assert result.rows[0].identifier_record.mapping_entry_count == 3
    assert result.rows[0].identifier_record.status == "UNRESOLVED_AMBIGUOUS"
    assert result.counts.associated_key_occurrence_count == 4
    assert result.counts.ambiguous_row_count == 2 and result.counts.raw_duplicate_row_count == 2


@pytest.mark.parametrize("explicit_negative", [False, True])
def test_cross_target_all_never_assembles_membership_from_different_targets(tmp_path, explicit_negative):
    entities = []
    for i in range(2):
        assertions = [assertion(category=("SYN_CAT_A", "SYN_CAT_B")[i], aid=f"SYN_POS_{i}")]
        if explicit_negative:
            assertions.append(assertion("ABSENT", ("SYN_CAT_B", "SYN_CAT_A")[i], scope="SYN_SCOPE_1", aid=f"SYN_NEG_{i}"))
        entities.append(entity(f"SYN_TARGET_{i}", assertions, f"SYN_ENTITY_{i}"))
    ref = annotation(entities, [scope(targets=("SYN_TARGET_0", "SYN_TARGET_1"))])
    m = mapping([("SYN_ALIAS", "SYN_TARGET_0"), ("SYN_ALIAS", "SYN_TARGET_1")])
    result = evaluate(tmp_path, ref, keys=("SYN_ALIAS",), mapping_ref=m, cats=("SYN_CAT_A", "SYN_CAT_B"))
    assert result.rows[0].truth == "U"
    assert [t.truth for t in result.rows[0].targets] == (["F", "F"] if explicit_negative else ["U", "U"])


def test_direct_raw_exact_preserves_lexical_policy_and_identity(tmp_path):
    ref = annotation([entity(assertions=[assertion()])])
    result = evaluate(tmp_path, ref, keys=(" syn_target_a ", "SYN_TARGET_A", "SYN_TARGET_A.2", "00123"),
                      column=contract(lexical_policy=dict(strip_whitespace=True, case="UPPER")))
    assert [r.truth for r in result.rows] == ["U", "T", "U", "U"]
    assert result.rows[0].identifier_record.lexical_identifier == "SYN_TARGET_A"
    assert result.rows[0].targets[0].identifier == " syn_target_a "
    assert all(r.identifier_record.status == "UNRESOLVED_NO_REFERENCE" for r in result.rows)
    assert result.identity_verification == "NOT_VERIFIED"


def test_mapping_uses_declared_alias_version_and_lexical_keys_without_fallback(tmp_path):
    ref = annotation([entity(assertions=[assertion()])])
    m = mapping([("SYN_ALIAS.2", "SYN_TARGET_A"), ("SYN_ALIAS", "SYN_TARGET_A")])
    result = evaluate(tmp_path, ref, keys=(" syn_alias.2 ", "SYN_ALIAS", "SYN_TARGET_A"), mapping_ref=m,
                      column=contract(m, lexical_policy=dict(strip_whitespace=True, case="UPPER")))
    assert [r.truth for r in result.rows] == ["T", "T", "U"]
    assert result.rows[-1].targets == ()
    assert result.rows[-1].identifier_record.status == "UNRESOLVED_NOT_FOUND"


def test_mapping_configuration_missing_binding_reference_and_direct_conflict(tmp_path):
    ref = annotation()
    m = mapping([])
    path = table(tmp_path)
    with pytest.raises(CategoryEvaluationError, match="MAPPING_BINDING_REQUIRED"):
        evaluate_categories(inspect_identifiers(path, contract()), annotation_reference=ref, conditions=condition(ref, mode="BOUND_MAPPING_TARGET"))
    bound = inspect_identifiers(path, contract(m))
    with pytest.raises(CategoryEvaluationError, match="MAPPING_REFERENCE_REQUIRED"):
        evaluate_categories(bound, annotation_reference=ref, conditions=condition(ref, mode="BOUND_MAPPING_TARGET"))
    with pytest.raises(CategoryEvaluationError, match="IDENTIFIER_REFERENCE_REPLAY_MISMATCH"):
        evaluate_categories(bound, annotation_reference=ref, conditions=condition(ref, mode="BOUND_MAPPING_TARGET"), mapping_reference=m)
    for identifiers in (bound, inspect_identifiers(path, contract(m), mapping_reference=m)):
        with pytest.raises(CategoryEvaluationError, match="DIRECT_MAPPING_CONFLICT"):
            evaluate_categories(identifiers, annotation_reference=ref, conditions=condition(ref))
    with pytest.raises(CategoryEvaluationError, match="DIRECT_MAPPING_CONFLICT"):
        evaluate_categories(inspect_identifiers(path, contract()), annotation_reference=ref, conditions=condition(ref), mapping_reference=m)


@pytest.mark.parametrize("field,value", [("namespace", "synthetic-other"), ("taxon", "synthetic-other"), ("release", "SYN_OTHER_RELEASE"), ("content_sha256", "0" * 64)])
def test_condition_binding_mismatch_fixed_codes(tmp_path, field, value):
    ref = annotation()
    binding = ref.binding.model_dump()
    binding[field] = value
    cond = condition(ref, annotation_binding=binding)
    with pytest.raises(CategoryEvaluationError, match="ANNOTATION_BINDING_MISMATCH"):
        evaluate_categories(inspect_identifiers(table(tmp_path), contract()), annotation_reference=ref, conditions=cond)


@pytest.mark.parametrize("field", ["namespace", "taxon"])
@pytest.mark.parametrize("mapped", [False, True])
def test_annotation_association_namespace_taxon_exact(tmp_path, field, mapped):
    ref = annotation(**{field: "synthetic-other"})
    m = mapping([]) if mapped else None
    with pytest.raises(CategoryEvaluationError, match="ANNOTATION_NAMESPACE_TAXON_MISMATCH"):
        evaluate(tmp_path, ref, mapping_ref=m)


def test_mapping_replay_detects_internally_valid_forged_targets_and_counts(tmp_path):
    m = mapping([("SYN_ALIAS", "SYN_TARGET_A")])
    original = inspect_identifiers(table(tmp_path, ("SYN_ALIAS",)), contract(m), mapping_reference=m)
    for updates in ({"mapping_targets": ["SYN_FORGED"]}, {"mapping_entry_count": 2}):
        payload = original.model_dump(mode="json")
        payload["identifiers"][0].update(updates)
        provisional = original.model_copy(update={"identifiers": tuple(type(original.identifiers[0])(**r) for r in payload["identifiers"])})
        payload["processing_id"] = "IDENTIFIERS_" + DatasetManifest.content_digest(provisional.identity_payload())
        forged = type(original).model_validate(payload)
        ref = annotation()
        with pytest.raises(CategoryEvaluationError, match="IDENTIFIER_REFERENCE_REPLAY_MISMATCH"):
            evaluate_categories(forged, annotation_reference=ref, conditions=condition(ref, mode="BOUND_MAPPING_TARGET"), mapping_reference=m)


@pytest.mark.parametrize("mutator", [
    lambda p: p["source_records"][0].update(source_binding=S2),
    lambda p: p["source_records"][0].update(source_binding=dict(source_id="SYN_UNDECLARED", release="SYN_RELEASE_1")),
    lambda p: p["entities"][0]["assertions"][0].update(complete_scope_id="SYN_MISSING_SCOPE"),
    lambda p: p["entities"][0]["assertions"][0].update(source_record_ids=["SYN_MISSING_RECORD"]),
    lambda p: p["entities"][0]["assertions"][0].update(category_id="SYN_UNKNOWN_CATEGORY"),
    lambda p: p["vocabulary"][0].update(definition_source="SYN_MISSING_RECORD"),
    lambda p: p["entities"].append(copy.deepcopy(p["entities"][0])),
    lambda p: p["vocabulary"].append(copy.deepcopy(p["vocabulary"][0])),
    lambda p: p["complete_scopes"].append(scope(status="PARTIAL", sid="SYN_CONTRADICTORY")),
    lambda p: p["complete_scopes"][0].update(category_ids=["SYN_UNKNOWN"]),
    lambda p: p["complete_scopes"][0].update(covered_identifiers=["SYN_TARGET_A", "SYN_TARGET_A"]),
])
def test_structural_references_and_contradictory_scope_rejected(mutator):
    payload = annotation([entity(assertions=[assertion()])], [scope()]).identity_payload()
    mutator(payload)
    with pytest.raises(ValidationError):
        AnnotationReference.from_declarations(**payload)


@pytest.mark.parametrize("updates", [dict(predicates=[]), dict(association_mode="AUTO"), dict(combine="NOT"),
    dict(direct_key_policy=None), dict(ambiguous_row_policy="CONSENSUS"), dict(unknown_policy="UNKNOWN_AS_FALSE"),
    dict(predicates=[dict(condition_id="SYN_COND", category_id="SYN_CAT_A", operator="NOT")]),
    dict(predicates=[dict(condition_id="SYN_COND", category_id="SYN_CAT_A")] * 2), dict(measurement_threshold=1)])
def test_unsupported_semantics_rejected(updates):
    with pytest.raises(ValidationError):
        condition(annotation(), **updates)


def test_missing_conditions_unknown_category_and_multiple_reference_rejected(tmp_path):
    ref = annotation()
    identifiers = inspect_identifiers(table(tmp_path), contract())
    with pytest.raises(CategoryEvaluationError, match="CATEGORY_CONDITIONS_REQUIRED"):
        evaluate_categories(identifiers, annotation_reference=ref, conditions=None)
    with pytest.raises(CategoryEvaluationError, match="CATEGORY_NOT_DECLARED"):
        evaluate_categories(identifiers, annotation_reference=ref, conditions=condition(ref, cats=("SYN_MISSPELLED",)))
    with pytest.raises(CategoryEvaluationError, match="INVALID_ANNOTATION_REFERENCE"):
        evaluate_categories(identifiers, annotation_reference=[ref, ref], conditions=condition(ref))


@pytest.mark.parametrize("loader,model", [(load_annotation_reference, "annotation"), (load_category_condition, "condition")])
@pytest.mark.parametrize("mutation", ["duplicate_top", "duplicate_nested", "digest", "unknown", "nan", "version", "wrong_type", "blank"])
def test_strict_loaders_sanitize_and_reject_corruption(loader, model, mutation):
    ref = annotation([entity(assertions=[assertion()])])
    original = ref if model == "annotation" else condition(ref)
    text = original.model_dump_json()
    payload = original.model_dump(mode="json")
    if mutation == "duplicate_top":
        text = '{"content_sha256":"SYN_PRIVATE",' + text[1:]
    elif mutation == "duplicate_nested":
        key = '"category_id":"SYN_CAT_A"'
        text = text.replace(key, '"category_id":"SYN_PRIVATE",' + key, 1)
    else:
        if mutation == "digest": payload["content_sha256"] = "0" * 64
        if mutation == "unknown": payload["SYN_PRIVATE_FIELD"] = "SYN_PRIVATE_VALUE"
        if mutation == "nan": payload["SYN_PRIVATE_FIELD"] = float("nan")
        if mutation == "version": payload["schema_version"] = "SYN_PRIVATE_VERSION"
        if mutation == "wrong_type": payload["content_sha256"] = 123
        if mutation == "blank":
            payload["reference_id" if model == "annotation" else "combine"] = " "
        text = json.dumps(payload)
    with pytest.raises(CategoryEvaluationError) as exc:
        loader(text.encode())
    assert "SYN_PRIVATE" not in str(exc.value)


def test_serialization_determinism_and_explicit_replay(tmp_path):
    ref = annotation([entity(assertions=[assertion()])])
    result = evaluate(tmp_path, ref)
    second = evaluate(tmp_path, ref)
    assert result.evaluation_id == second.evaluation_id
    loaded = load_evaluation_result(result.model_dump_json())
    assert loaded == result
    assert replay_evaluation_result(loaded, annotation_reference=ref, conditions=result.conditions) == result
    external = evaluate_categories(result.identifiers, annotation_reference=ref, conditions=result.conditions)
    assert external.rows == result.rows
    assert external.validation_level == "SERIALIZED_INPUT_REFERENCE_REPLAY"
    assert external.evaluation_id != result.evaluation_id
    assert AnnotationReference.model_json_schema()["additionalProperties"] is False
    assert CategoryCondition.model_json_schema()["additionalProperties"] is False
    assert EvaluationResult.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize("field", ["counts", "rows", "input_binding", "evaluation_id", "condition_binding", "data_classification"])
def test_result_structural_validation_rejects_tampering(tmp_path, field):
    result = evaluate(tmp_path, annotation([entity(assertions=[assertion()])]))
    payload = result.model_dump(mode="json")
    if field == "counts": payload[field]["row_outcomes"]["INCLUDED"] = 0
    if field == "rows": payload[field][0]["targets"][0]["identifier"] = "SYN_FORGED"
    if field == "input_binding": payload[field]["source_file_size"] += 1
    if field == "evaluation_id": payload[field] = "SYN_FORGED"
    if field == "condition_binding": payload[field] = "0" * 64
    if field == "data_classification": payload[field] = "PUBLIC"
    with pytest.raises(CategoryEvaluationError, match="INVALID_CATEGORY_EVALUATION"):
        load_evaluation_result(json.dumps(payload))


def test_structure_loading_does_not_claim_reference_semantics_or_current_source_bytes(tmp_path):
    ref = annotation([entity(assertions=[assertion()])])
    result = evaluate(tmp_path, ref)
    # A self-consistent, rehashed fabricated category state passes structure,
    # then fails separately supplied reference replay. Digests are not signatures.
    payload = result.model_dump(mode="json")
    p = payload["rows"][0]["targets"][0]["predicates"][0]
    p.update(category_state="UNCERTAIN", truth="U")
    p["evidence"][0].update(applicability="UNDETERMINED", reasons=["UNCERTAIN_ASSERTION"])
    p["reasons"].append("UNCERTAIN_ASSERTION")
    payload["rows"][0]["targets"][0].update(truth="U", decisive_condition_ids=[])
    payload["rows"][0].update(truth="U", outcome="UNKNOWN", reasons=p["reasons"])
    provisional = EvaluationResult.model_construct(**{
        **result.__dict__, "rows": tuple(categories.RowEvaluation(**r) for r in payload["rows"])})
    payload["counts"] = categories._counts(result.identifiers, provisional.rows, result.conditions).model_dump(mode="json")
    provisional = EvaluationResult.model_construct(**{**provisional.__dict__, "counts": categories.EvaluationCounts(**payload["counts"])})
    payload["evaluation_id"] = "CATEGORIES_" + DatasetManifest.content_digest(provisional.identity_payload())
    loaded = load_evaluation_result(json.dumps(payload))
    with pytest.raises(CategoryEvaluationError, match="CATEGORY_REFERENCE_REPLAY_MISMATCH"):
        replay_evaluation_result(loaded, annotation_reference=ref, conditions=result.conditions)
    path = tmp_path / "synthetic.csv"
    path.write_text("synthetic_feature,synthetic_value\nSYN_REPLACED,7\n")
    assert replay_evaluation_result(result, annotation_reference=ref, conditions=result.conditions) == result
    current = inspect_and_evaluate_categories(path, contract(), annotation_reference=ref, conditions=result.conditions)
    assert current.input_binding.processing_id != result.input_binding.processing_id


def test_source_mutated_after_table_snapshot_keeps_original_bytes(tmp_path, monkeypatch):
    path = table(tmp_path)
    raw = path.read_bytes()
    reader = tables.read_evidence_rows
    def mutate(snapshot, **kwargs):
        path.write_text("synthetic_feature,synthetic_value\nSYN_CHANGED,900\n")
        return reader(snapshot, **kwargs)
    monkeypatch.setattr(tables, "read_evidence_rows", mutate)
    ref = annotation([entity(assertions=[assertion()])])
    result = inspect_and_evaluate_categories(path, contract(), annotation_reference=ref, conditions=condition(ref))
    assert result.rows[0].identifier_record.raw_identifier == "SYN_TARGET_A"
    assert result.input_binding.source_file_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.rows[0].truth == "T"


def write_declarations(tmp_path, ref, cond, m=None):
    paths = []
    for name, value in (("contract", contract(m)), ("annotation", ref), ("condition", cond)):
        path = tmp_path / ("synthetic_" + name + ".json")
        path.write_text(value.model_dump_json())
        paths.append(path)
    return paths


def test_file_entry_reads_declarations_once_and_freezes_before_later_replacement(tmp_path, monkeypatch):
    ref = annotation([entity(assertions=[assertion()])])
    cond = condition(ref)
    cpath, apath, qpath = write_declarations(tmp_path, ref, cond)
    original_read = categories._read_local_declaration
    calls = []
    def read_and_replace(path, limit):
        raw = original_read(path, limit)
        calls.append(Path(path))
        Path(path).write_text("SYN_CHANGED_INVALID")
        return raw
    monkeypatch.setattr(categories, "_read_local_declaration", read_and_replace)
    result = evaluate_category_files(table(tmp_path), cpath, annotation_reference_path=apath, condition_path=qpath)
    assert calls == [cpath, apath, qpath]
    assert result.rows[0].truth == "T"
    assert result.annotation_reference == ref and result.conditions == cond


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_defaults_and_json_only_counts_fixed_statuses(tmp_path, capsys, monkeypatch, json_mode):
    ref = annotation([entity("SYN_PRIVATE_IDENTIFIER", [assertion()])], reference_id="SYN_PRIVATE_REFERENCE")
    cond = condition(ref)
    cpath, apath, qpath = write_declarations(tmp_path, ref, cond)
    argv = ["SYN_PRIVATE_PROGRAM", str(table(tmp_path, ("SYN_PRIVATE_IDENTIFIER",))), "--column-contract", str(cpath),
            "--annotation-reference", str(apath), "--category-condition", str(qpath)]
    monkeypatch.setattr(sys, "argv", argv + (["--json"] if json_mode else []))
    assert main() == 0
    output = capsys.readouterr()
    assert not output.err
    for secret in ("SYN_PRIVATE", "synthetic_feature", "synthetic_value", "Invented", str(tmp_path), ref.content_sha256, cond.content_sha256):
        assert secret not in output.out
    if json_mode:
        assert json.loads(output.out)["row_outcomes"] == dict(INCLUDED=1, EXCLUDED=0, UNKNOWN=0)


@pytest.mark.parametrize("fault", ["missing_condition", "missing_contract", "missing_annotation", "invalid_annotation", "invalid_condition", "unavailable_annotation"])
def test_cli_errors_never_echo_private_data(tmp_path, capsys, monkeypatch, fault):
    ref = annotation()
    cpath, apath, qpath = write_declarations(tmp_path, ref, condition(ref))
    if fault == "invalid_annotation": apath.write_text('{"SYN_PRIVATE_FIELD":"SYN_PRIVATE_VALUE"}')
    if fault == "invalid_condition": qpath.write_text('{"SYN_PRIVATE_FIELD":"SYN_PRIVATE_VALUE"}')
    if fault == "unavailable_annotation": apath = tmp_path / "SYN_PRIVATE_MISSING"
    argv = ["SYN_PRIVATE_PROGRAM", str(table(tmp_path))]
    for flag, path, omission in (("--column-contract", cpath, "missing_contract"), ("--annotation-reference", apath, "missing_annotation"), ("--category-condition", qpath, "missing_condition")):
        if fault != omission: argv.extend((flag, str(path)))
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert not output.out and "SYN_PRIVATE" not in output.err and str(tmp_path) not in output.err


def test_annotation_order_provenance_and_condition_semantics_bound_in_digest(tmp_path):
    ref = annotation([entity(assertions=[assertion(), assertion(aid="SYN_ASSERT_2")])])
    original = evaluate(tmp_path, ref)
    payload = ref.identity_payload()
    payload["entities"][0]["assertions"].reverse()
    changed = AnnotationReference.from_declarations(**payload)
    assert changed.content_sha256 != ref.content_sha256
    assert evaluate(tmp_path, changed).evaluation_id != original.evaluation_id
    payload = ref.identity_payload()
    payload["source_records"][0]["source_locator"] = "SYN_DIFFERENT_PROVENANCE"
    assert AnnotationReference.from_declarations(**payload).content_sha256 != ref.content_sha256
    any_result = evaluate(tmp_path, ref, combine="ANY")
    assert any_result.rows == original.rows and any_result.evaluation_id != original.evaluation_id


def test_bounded_declaration_files_and_unvalidated_instance_rechecked(tmp_path):
    path = tmp_path / "synthetic.json"
    path.write_bytes(b"12345")
    with pytest.raises(CategoryEvaluationError, match="DECLARATION_BYTE_LIMIT"):
        categories._read_local_declaration(path, 4)
    ref = annotation()
    corrupted = ref.model_copy(update={"namespace": " "})
    with pytest.raises(CategoryEvaluationError, match="INVALID_ANNOTATION_REFERENCE"):
        inspect_and_evaluate_categories(table(tmp_path), contract(), annotation_reference=corrupted, conditions=condition(ref))


@pytest.mark.parametrize("second_targets,second_cats,second_source,conflict", [
    (("SYN_TARGET_A",), ("SYN_CAT_A",), S1, True),
    (("SYN_TARGET_B",), ("SYN_CAT_A",), S1, False),
    (("SYN_TARGET_A",), ("SYN_CAT_B",), S1, False),
    (("SYN_TARGET_A",), ("SYN_CAT_A",), S2, False),
    (("SYN_TARGET_A",), ("SYN_CAT_A",), S1_OTHER, False),
])
def test_scope_contradiction_needs_both_set_overlaps_and_same_source_release(second_targets, second_cats, second_source, conflict):
    scopes = [scope(targets=("SYN_TARGET_A",), cats=("SYN_CAT_A",)),
              scope(targets=second_targets, cats=second_cats, source=second_source, status="PARTIAL", sid="SYN_SECOND_SCOPE")]
    if conflict:
        with pytest.raises(ValidationError, match="CATEGORY_SCOPE_CONTRADICTION"):
            annotation(scopes=scopes)
    else:
        assert len(annotation(scopes=scopes).complete_scopes) == 2


def test_reasonable_enumerated_scope_keeps_membership_scope_order_and_contradiction_checks(tmp_path):
    # 800 × 200 conceptual pairs need only 800 + 200 membership values.
    targets = tuple(f"SYN_LARGE_TARGET_{i}" for i in range(800))
    cats = tuple(f"SYN_LARGE_CAT_{i}" for i in range(200))
    vocabulary = [dict(category_id=c, label="Invented", definition="Invented test set", definition_source="SYN_RECORD_1") for c in cats]
    scopes = [scope(targets=targets, cats=cats, sid="SYN_LARGE_SCOPE"),
              scope(targets=(targets[-1],), cats=(cats[-1],), source=S2, status="PARTIAL", sid="SYN_SECOND_SCOPE")]
    ref = annotation([entity(targets[-1], [assertion("ABSENT", cats[-1], scope="SYN_LARGE_SCOPE")])], scopes,
                     vocabulary=vocabulary)
    result = evaluate(tmp_path, ref, keys=(targets[-1],), cats=(cats[-1],))
    assert predicate(result).covering_scope_ids == ("SYN_LARGE_SCOPE", "SYN_SECOND_SCOPE")
    assert result.rows[0].truth == "F"
    with pytest.raises(ValidationError, match="CATEGORY_SCOPE_CONTRADICTION"):
        annotation(scopes=[*scopes, scope(targets=(targets[-1],), cats=(cats[-1],), status="UNKNOWN", sid="SYN_CONTRADICTORY")], vocabulary=vocabulary)


def test_duplicate_sources_entities_and_assertions_keep_declared_order(tmp_path):
    ref = annotation([entity(assertions=[assertion(aid="SYN_ASSERT_A"), assertion(aid="SYN_ASSERT_B")]),
                      entity(assertions=[assertion(source=S2, aid="SYN_ASSERT_C")], eid="SYN_ENTITY_2")])
    result = evaluate(tmp_path, ref, keys=("SYN_TARGET_A", "SYN_TARGET_A"))
    assert [e.assertion.assertion_id for e in predicate(result).evidence] == ["SYN_ASSERT_A", "SYN_ASSERT_B", "SYN_ASSERT_C"]
    assert [e.entity_record_id for e in predicate(result).evidence] == ["SYN_ENTITY_1", "SYN_ENTITY_1", "SYN_ENTITY_2"]
    assert result.counts.input_row_count == 2 and result.counts.row_outcomes["INCLUDED"] == 2
    assert result.rows[0].targets == result.rows[1].targets


def test_conflict_is_unknown_at_predicate_and_remains_auditable_when_any_is_true(tmp_path):
    ref = annotation([entity(assertions=[assertion(), assertion("ABSENT", scope="SYN_SCOPE_1", aid="SYN_NEG"),
                                        assertion(category="SYN_CAT_B", aid="SYN_CAT_B_POS")])], [scope()])
    result = evaluate(tmp_path, ref, combine="ANY", cats=("SYN_CAT_A", "SYN_CAT_B"))
    assert [p.truth for p in result.rows[0].targets[0].predicates] == ["U", "T"]
    assert result.rows[0].truth == "T" and result.counts.row_outcomes["INCLUDED"] == 1
    assert result.conflicts == result.confirmed_matches
    assert result.counts.conflict_row_count == 1
    assert result.rows[0].targets[0].decisive_condition_ids == ("SYN_COND_1",)


def test_complete_scope_does_not_negate_missing_assertion_in_all(tmp_path):
    ref = annotation([entity(assertions=[assertion()])], [scope()])
    result = evaluate(tmp_path, ref, cats=("SYN_CAT_A", "SYN_CAT_B"))
    assert [p.truth for p in result.rows[0].targets[0].predicates] == ["T", "U"]
    assert result.rows[0].truth == "U"


def test_paths_and_operational_time_do_not_change_evaluation_identity(tmp_path):
    ref = annotation([entity(assertions=[assertion()])])
    cond = condition(ref)
    first = inspect_and_evaluate_categories(table(tmp_path), contract(), annotation_reference=ref, conditions=cond, inspected_at=STAMP)
    second = inspect_and_evaluate_categories(table(tmp_path, name="synthetic_renamed.csv"), contract(), annotation_reference=ref,
                                            conditions=cond, inspected_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert first.evaluation_id == second.evaluation_id


def test_bound_mapping_file_read_once_and_reference_replay_requires_matching_reference(tmp_path, monkeypatch):
    m = mapping([("SYN_ALIAS", "SYN_TARGET_A")])
    ref = annotation([entity(assertions=[assertion()])])
    cond = condition(ref, mode="BOUND_MAPPING_TARGET")
    cpath, apath, qpath = write_declarations(tmp_path, ref, cond, m)
    mpath = tmp_path / "synthetic_mapping.json"
    mpath.write_text(m.model_dump_json())
    original_read = categories._read_local_declaration
    calls = []
    def read_and_replace(path, limit):
        raw = original_read(path, limit)
        calls.append(Path(path))
        Path(path).write_text("SYN_INVALID_REPLACEMENT")
        return raw
    monkeypatch.setattr(categories, "_read_local_declaration", read_and_replace)
    result = evaluate_category_files(table(tmp_path, ("SYN_ALIAS",)), cpath, annotation_reference_path=apath,
                                    condition_path=qpath, mapping_reference_path=mpath)
    assert calls == [cpath, mpath, apath, qpath]
    assert result.rows[0].truth == "T"
    assert replay_evaluation_result(result, annotation_reference=ref, conditions=cond, mapping_reference=m) == result
    with pytest.raises(CategoryEvaluationError, match="MAPPING_REFERENCE_REQUIRED"):
        replay_evaluation_result(result, annotation_reference=ref, conditions=cond)
    wrong = mapping([("SYN_ALIAS", "SYN_OTHER_TARGET")])
    with pytest.raises(CategoryEvaluationError, match="MAPPING_REFERENCE_BINDING_MISMATCH"):
        replay_evaluation_result(result, annotation_reference=ref, conditions=cond, mapping_reference=wrong)


def test_schema_and_api_have_no_scoring_consensus_join_or_provider_controls():
    import ast
    for module in (categories, sys.modules[AnnotationReference.__module__]):
        tree = ast.parse(Path(module.__file__).read_text())
        imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        assert not any(part in module_name for module_name in imports for part in ("sqlite", "socket", "requests", "provider", "decision", "interpretation"))
    fields = set(CategoryCondition.model_fields) | set(EvaluationResult.model_fields)
    assert not fields.intersection({"score", "rank", "consensus", "provider", "join", "threshold"})


@pytest.mark.parametrize("state", ["PRESENT", "UNCERTAIN"])
def test_positive_and_uncertain_optional_applicability_scope_is_respected(tmp_path, state):
    ref = annotation([entity(assertions=[assertion(state, scope="SYN_SCOPE_1")])],
                     [scope(targets=("SYN_OTHER_TARGET",))])
    result = evaluate(tmp_path, ref)
    assert result.rows[0].truth == "U"
    evidence = predicate(result).evidence[0]
    assert evidence.applicability == "INAPPLICABLE"
    assert evidence.reasons == ("OUTSIDE_ASSERTION_SCOPE",)


def test_all_partitions_and_local_views_keep_original_multiline_locators(tmp_path):
    ref = annotation([entity(assertions=[assertion()]),
                      entity("SYN_TARGET_B", [assertion("ABSENT", scope="SYN_SCOPE_1", aid="SYN_NEG")], "SYN_ENTITY_B")], [scope()])
    result = evaluate(tmp_path, ref, keys=("SYN_TARGET_A", "SYN_TARGET_B", "SYN_multiline\nidentifier", "SYN_TARGET_A"))
    assert result.counts.row_outcomes == dict(INCLUDED=2, EXCLUDED=1, UNKNOWN=1)
    assert len(result.confirmed_matches) == 2 and len(result.nonmatches) == len(result.unknowns) == 1
    assert result.unknowns[0].identifier_record.raw_identifier == "SYN_multiline\nidentifier"
    assert result.unknowns[0].identifier_record.locator.physical_line_end == 5
    assert result.rows[-1].identifier_record.locator.logical_record == 4


@pytest.mark.parametrize("suffix,delimiter,encoding", [(".csv", ",", "utf-8-sig"), (".tsv", "\t", "utf-16")])
def test_category_wrapper_reuses_existing_formats_encodings_and_limits(tmp_path, suffix, delimiter, encoding):
    path = tmp_path / ("synthetic" + suffix)
    path.write_bytes(f"synthetic_feature{delimiter}synthetic_value\n00123{delimiter}7\n".encode(encoding))
    ref = annotation([entity("00123", [assertion()])])
    result = inspect_and_evaluate_categories(path, contract(), annotation_reference=ref, conditions=condition(ref), encoding=encoding)
    assert result.rows[0].truth == "T" and result.rows[0].identifier_record.raw_identifier == "00123"
    assert result.identifiers.inspection.manifest.input_format == ("CSV" if suffix == ".csv" else "TSV")
    with pytest.raises(categories.DatasetInspectionError, match="BYTE_LIMIT"):
        inspect_and_evaluate_categories(path, contract(), annotation_reference=ref, conditions=condition(ref), encoding=encoding, max_bytes=1)


def test_result_loader_rejects_duplicate_keys_and_nonstandard_constants(tmp_path):
    result = evaluate(tmp_path, annotation())
    text = result.model_dump_json()
    for altered in ('{"evaluation_id":"SYN_PRIVATE",' + text[1:],
                    text.replace('"truth":"U"', '"truth":"SYN_PRIVATE","truth":"U"', 1),
                    '{"SYN_PRIVATE":NaN}'):
        with pytest.raises(CategoryEvaluationError, match="INVALID_CATEGORY_EVALUATION") as exc:
            load_evaluation_result(altered)
        assert "SYN_PRIVATE" not in str(exc.value)


def test_unvalidated_nested_legacy_model_copy_is_revalidated_at_6b3_boundaries(tmp_path):
    ref = annotation()
    identifiers = inspect_identifiers(table(tmp_path), contract())
    bad_manifest = identifiers.inspection.manifest.model_copy(update={"source_file_sha256": "0" * 64})
    bad_inspection = identifiers.inspection.model_copy(update={"manifest": bad_manifest})
    corrupted = identifiers.model_copy(update={"inspection": bad_inspection})
    with pytest.raises(CategoryEvaluationError, match="INVALID_IDENTIFIER_INSPECTION"):
        evaluate_categories(corrupted, annotation_reference=ref, conditions=condition(ref))
    result = evaluate(tmp_path, ref)
    corrupted_result = result.model_copy(update={"identifiers": corrupted})
    with pytest.raises(CategoryEvaluationError, match="INVALID_CATEGORY_EVALUATION"):
        replay_evaluation_result(corrupted_result, annotation_reference=ref, conditions=result.conditions)


@pytest.mark.parametrize("nested_at", ["inspection", "manifest"])
def test_identifier_mapping_cannot_hide_unvalidated_legacy_inspection_or_manifest(tmp_path, nested_at, capsys):
    ref = annotation()
    valid = inspect_identifiers(table(tmp_path), contract())
    bad_manifest = valid.inspection.manifest.model_copy(update={"source_file_sha256": "0" * 64})
    payload = valid.model_dump(mode="python")
    if nested_at == "inspection":
        payload["inspection"] = valid.inspection.model_copy(update={"manifest": bad_manifest})
    else:
        payload["inspection"]["manifest"] = bad_manifest
    with pytest.raises(CategoryEvaluationError) as exc:
        evaluate_categories(payload, annotation_reference=ref, conditions=condition(ref))
    assert exc.value.code == "INVALID_IDENTIFIER_INSPECTION"
    assert str(exc.value) == "INVALID_IDENTIFIER_INSPECTION: local category declaration or evaluation failed"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("nested_at", ["identifiers", "inspection"])
def test_result_mapping_cannot_hide_unvalidated_identifiers_even_with_rehashed_binding(tmp_path, nested_at, capsys):
    ref = annotation()
    result = evaluate(tmp_path, ref)
    valid = result.identifiers
    bad_manifest = valid.inspection.manifest.model_copy(update={"source_file_sha256": "0" * 64})
    bad_table = valid.inspection.model_copy(update={"manifest": bad_manifest})
    bad_identifiers = valid.model_copy(update={"inspection": bad_table})
    bad_binding = result.input_binding.model_copy(update={"source_file_sha256": "0" * 64})
    forged = result.model_copy(update={"identifiers": bad_identifiers, "input_binding": bad_binding})
    forged_id = "CATEGORIES_" + DatasetManifest.content_digest(forged.identity_payload())
    payload = result.model_dump(mode="python")
    payload["input_binding"] = bad_binding
    payload["evaluation_id"] = forged_id
    if nested_at == "identifiers":
        payload["identifiers"] = bad_identifiers
    else:
        payload["identifiers"]["inspection"] = bad_table
    with pytest.raises(CategoryEvaluationError) as exc:
        replay_evaluation_result(payload, annotation_reference=ref, conditions=result.conditions)
    assert exc.value.code == "INVALID_CATEGORY_EVALUATION"
    assert capsys.readouterr() == ("", "")


def test_legal_mixed_mapping_and_model_inputs_preserve_evaluation_and_replay(tmp_path):
    ref = annotation([entity(assertions=[assertion()])])
    cond = condition(ref)
    valid = inspect_identifiers(table(tmp_path), contract())
    expected = evaluate_categories(valid, annotation_reference=ref, conditions=cond)
    payload = valid.model_dump(mode="python")
    payload.update(inspection=valid.inspection, column_contract=valid.column_contract,
                   identifiers=valid.identifiers)
    mixed = evaluate_categories(payload, annotation_reference=ref, conditions=cond)
    assert mixed.evaluation_id == expected.evaluation_id and mixed.rows == expected.rows
    mixed_result = mixed.model_dump(mode="python")
    mixed_result.update(identifiers=mixed.identifiers, annotation_reference=ref,
                        conditions=cond, rows=mixed.rows, input_binding=mixed.input_binding)
    assert replay_evaluation_result(mixed_result, annotation_reference=ref, conditions=cond) == mixed
