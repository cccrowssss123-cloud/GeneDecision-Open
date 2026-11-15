"""Single-snapshot and aggregation tests with invented software fixtures only."""

import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from genedecision.discovery import inspect_and_evaluate_categories, inspect_measurements, run, tables
from genedecision.discovery.models import DatasetManifest
from genedecision.discovery.numeric import evaluate_numeric_conditions
from genedecision.discovery.run import CandidateKey, DiscoveryRun, DiscoveryRunError, run_discovery
from test_discovery_categories import annotation, assertion, condition as category_condition, entity, mapping, scope
from test_discovery_measurements import STAMP, audit_contract, contract
from test_discovery_numeric import condition as numeric_condition, context as numeric_context
from test_discovery_tables import no_runtime_dependencies  # shared local-only guard

pytestmark = pytest.mark.usefixtures("no_runtime_dependencies")


def write_table(tmp_path, rows, name="synthetic.csv"):
    path = tmp_path / name
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("synthetic_value", "synthetic_feature", "synthetic_unselected"))
        writer.writerows(rows)
    return path


def mixed_fixture(tmp_path):
    rows = (("8", "SYN_A", "  raw note  "), ("6", "SYN_ALIAS_A", "line 1\nline 2"),
            ("", "SYN_A", "001"), ("8", "SYN_AMBIGUOUS", ""), ("8", "SYN_UNANNOTATED", "NA"),
            ("8", "SYN_B", "x,y"), ("6", "SYN_UNMAPPED", "tail"))
    path = write_table(tmp_path, rows)
    ref = mapping((("SYN_A", "SYN_TARGET_A"), ("SYN_ALIAS_A", "SYN_TARGET_A"),
                   ("SYN_AMBIGUOUS", "SYN_TARGET_A"), ("SYN_AMBIGUOUS", "SYN_TARGET_B"),
                   ("SYN_UNANNOTATED", "SYN_TARGET_UNANNOTATED"), ("SYN_B", "SYN_TARGET_B")))
    column = contract(mapping=ref)
    annotation_ref = annotation(
        [entity(assertions=[assertion()]),
         entity("SYN_TARGET_B", [assertion("ABSENT", aid="SYN_ABSENT", scope="SYN_SCOPE_1")], "SYN_ENTITY_B")],
        [scope()],
    )
    category = category_condition(annotation_ref, mode="BOUND_MAPPING_TARGET")
    audit = audit_contract(column)
    measured = inspect_measurements(path, column, audit, mapping_reference=ref, inspected_at=STAMP)
    context = numeric_context(measured, ("DECLARED_FAIL",), scope="ROWS", locators=(measured.rows[0].identifier_record.locator,))
    numeric = numeric_condition(measured, context=context)
    kwargs = dict(mapping_reference=ref, annotation_reference=annotation_ref, category_condition=category,
                  measurement_audit_contract=audit, numeric_condition=numeric, numeric_context=context)
    return path, column, rows, measured, kwargs


@pytest.mark.parametrize("combine, truths", [("ALL", "TFUUUFF"), ("ANY", "TTTTTTU")])
def test_both_conditions_preserve_raw_fields_upstream_outcomes_and_ambiguous_rows(tmp_path, combine, truths):
    path, column, raw, measured, kwargs = mixed_fixture(tmp_path)
    result = run_discovery(path, column, **kwargs, combine=combine, inspected_at=STAMP)
    assert tuple(row.raw_cells for row in result.observations) == raw
    assert result.identifiers.inspection.manifest.ordered_column_names == (
        "synthetic_value", "synthetic_feature", "synthetic_unselected")
    assert result.observations[1].locator.physical_line_end == 4
    assert [r.truth for r in result.observations] == list(truths)
    assert [r.category_truth for r in result.observations] == list("TTTUUFU")
    assert [r.numeric_truth for r in result.observations] == list("TFUTTTF")
    assert result.category_evaluation.rows[3].identifier_record.mapping_targets == ("SYN_TARGET_A", "SYN_TARGET_B")
    assert result.observations[3].canonical_key is None
    assert result.numeric_evaluation.rows[0].has_declared_qc_failure
    assert result.numeric_evaluation.rows[0].warning == result.warning == "SCIENTIFICALLY_UNVERIFIED"
    assert result.readiness == "NOT_ASSESSED"
    assert result.numeric_evaluation.measurement_audit.audit_id == measured.audit_id
    expected_numeric = evaluate_numeric_conditions(measured, kwargs["numeric_condition"], context=kwargs["numeric_context"])
    assert result.numeric_evaluation.evaluation_id == expected_numeric.evaluation_id
    expected_category = inspect_and_evaluate_categories(
        path, column, mapping_reference=kwargs["mapping_reference"], annotation_reference=kwargs["annotation_reference"],
        conditions=kwargs["category_condition"], inspected_at=STAMP,
    )
    assert result.category_evaluation.evaluation_id == expected_category.evaluation_id
    first = result.candidates[0]
    assert first.key == CandidateKey(taxon="synthetic-taxon", namespace="synthetic-target", identifier="SYN_TARGET_A")
    assert [r.logical_record for r in first.observed_rows] == [1, 2, 3]
    assert [r.logical_record for r in first.matching_rows] == ([1] if combine == "ALL" else [1, 2, 3])
    assert [c.key.identifier for c in result.candidates] == (
        ["SYN_TARGET_A"] if combine == "ALL" else ["SYN_TARGET_A", "SYN_TARGET_UNANNOTATED", "SYN_TARGET_B"])


def test_combined_run_opens_source_once_and_reads_only_captured_snapshot(tmp_path, monkeypatch):
    path, column, raw, _, kwargs = mixed_fixture(tmp_path)
    original_bytes = path.read_bytes()
    original_open = Path.open
    original_reader = tables.read_evidence_rows
    reads = []

    def track_open(self, mode="r", *args, **options):
        if self == path and mode == "rb":
            reads.append(self)
        return original_open(self, mode, *args, **options)

    def replace_original(snapshot, **options):
        assert snapshot != path
        path.write_text("synthetic_value,synthetic_feature,synthetic_unselected\n6,SYN_CHANGED,changed\n")
        return original_reader(snapshot, **options)

    monkeypatch.setattr(Path, "open", track_open)
    monkeypatch.setattr(tables, "read_evidence_rows", replace_original)
    result = run_discovery(path, column, **kwargs, combine="ALL",
                           expected_source_sha256=hashlib.sha256(original_bytes).hexdigest())
    assert reads == [path]
    assert tuple(row.raw_cells for row in result.observations) == raw
    assert result.identifiers.inspection.manifest.source_file_sha256 == hashlib.sha256(original_bytes).hexdigest()


def test_category_raw_exact_rows_remain_separate_and_do_not_create_candidates(tmp_path):
    path = write_table(tmp_path, (("1", "SYN_TARGET_A", ""), ("2", "SYN_TARGET_A", ""), ("3", " SYN_TARGET_A ", "")))
    column = contract(namespace="synthetic-target", lexical_policy=dict(strip_whitespace=True))
    ref = annotation([entity(assertions=[assertion()])])
    result = run_discovery(path, column, annotation_reference=ref, category_condition=category_condition(ref))
    assert [r.truth for r in result.observations] == ["T", "T", "U"]
    assert all(r.canonical_key is None and r.numeric_truth is None for r in result.observations)
    assert result.candidates == () and result.numeric_evaluation is None and result.combine is None


def test_numeric_only_repeated_ambiguous_and_blank_rows_match_without_candidates(tmp_path):
    path = write_table(tmp_path, (("8", "SYN_AMBIGUOUS", ""), ("8", "SYN_AMBIGUOUS", "repeated"), ("8", "", "")))
    ref = mapping((("SYN_AMBIGUOUS", "SYN_TARGET_A"), ("SYN_AMBIGUOUS", "SYN_TARGET_B")))
    column = contract(mapping=ref)
    audit = audit_contract(column)
    measured = inspect_measurements(path, column, audit, mapping_reference=ref)
    result = run_discovery(path, column, mapping_reference=ref, measurement_audit_contract=audit,
                           numeric_condition=numeric_condition(measured))
    assert [r.truth for r in result.observations] == ["T", "T", "T"]
    assert [r.status for r in result.identifiers.identifiers] == ["UNRESOLVED_AMBIGUOUS", "UNRESOLVED_AMBIGUOUS", "BLANK"]
    assert result.candidates == () and result.category_evaluation is None


@pytest.mark.parametrize("missing, code", [
    ("both", "DISCOVERY_CONDITION_REQUIRED"), ("category_condition", "DISCOVERY_CATEGORY_DECLARATIONS_INCOMPLETE"),
    ("annotation_reference", "DISCOVERY_CATEGORY_DECLARATIONS_INCOMPLETE"),
    ("measurement_audit_contract", "DISCOVERY_NUMERIC_DECLARATIONS_INCOMPLETE"),
    ("numeric_condition", "DISCOVERY_NUMERIC_DECLARATIONS_INCOMPLETE"),
    ("combine", "DISCOVERY_COMBINE_REQUIRED"), ("single_combine", "DISCOVERY_COMBINE_NOT_APPLICABLE"),
    ("unrelated_context", "DISCOVERY_NUMERIC_DECLARATIONS_INCOMPLETE"),
])
def test_incomplete_or_unrelated_declarations_are_rejected_before_source_access(tmp_path, missing, code):
    path, column, _, _, kwargs = mixed_fixture(tmp_path)
    kwargs["combine"] = "ALL"
    if missing == "both":
        kwargs = {}
    elif missing in ("single_combine", "unrelated_context"):
        kwargs.pop("numeric_condition")
        kwargs.pop("measurement_audit_contract")
        if missing == "single_combine":
            kwargs.pop("numeric_context")
    else:
        kwargs.pop(missing)
    with pytest.raises(DiscoveryRunError, match=code) as failure:
        run_discovery(path.with_name("SYN_PRIVATE_MISSING.csv"), column, **kwargs)
    assert "SYN_PRIVATE" not in str(failure.value)


def test_model_round_trip_and_id_stable_across_times_paths_but_binds_all_columns(tmp_path):
    path, column, raw, _, kwargs = mixed_fixture(tmp_path)
    first = run_discovery(path, column, **kwargs, combine="ALL", inspected_at=STAMP)
    second = run_discovery(write_table(tmp_path, raw, name="renamed.csv"), column, **kwargs, combine="ALL",
                           inspected_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert first.run_id == second.run_id
    assert first.run_id != run_discovery(path, column, **kwargs, combine="ANY").run_id
    assert DiscoveryRun.model_validate_json(first.model_dump_json()) == first
    # Changing an unselected raw cell changes the run identity even though all
    # filter outcomes and identifier strings are unchanged.
    category_kwargs = {field: kwargs[field] for field in ("mapping_reference", "annotation_reference", "category_condition")}
    before = run_discovery(path, column, **category_kwargs)
    changed = ((raw[0][0], raw[0][1], "synthetic replacement"), *raw[1:])
    after = run_discovery(write_table(tmp_path, changed, name="changed.csv"), column, **category_kwargs)
    assert before.run_id != after.run_id
    assert [r.truth for r in before.observations] == [r.truth for r in after.observations]


@pytest.mark.parametrize("change", ["raw_identifier", "raw_measurement", "row_order", "truth", "key", "candidate", "locator", "mapping"])
def test_model_rejects_inconsistent_rehashed_rows_and_candidates(tmp_path, change):
    path, column, _, _, kwargs = mixed_fixture(tmp_path)
    result = run_discovery(path, column, **kwargs, combine="ALL")
    row = result.observations[0]
    changes = {}
    if change == "raw_identifier":
        changes["observations"] = (row.model_copy(update={"raw_cells": ("8", "SYN_FORGED", row.raw_cells[2])}), *result.observations[1:])
    if change == "raw_measurement":
        changes["observations"] = (row.model_copy(update={"raw_cells": ("9", *row.raw_cells[1:])}), *result.observations[1:])
    if change == "row_order":
        changes["observations"] = tuple(reversed(result.observations))
    if change == "truth":
        changes["observations"] = (row.model_copy(update={"truth": "F"}), *result.observations[1:])
    if change == "key":
        changes["observations"] = (row.model_copy(update={"canonical_key": None}), *result.observations[1:])
    if change == "candidate":
        changes["candidates"] = (result.candidates[0].model_copy(update={"matching_rows": result.candidates[0].observed_rows}),)
    if change == "locator":
        bad_locator = row.locator.model_copy(update={"logical_record": True})
        changes["observations"] = (row.model_copy(update={"locator": bad_locator}), *result.observations[1:])
    if change == "mapping":
        changes["mapping_reference"] = mapping((("SYN_A", "SYN_DIFFERENT"),))
    modified = result.model_copy(update=changes)
    payload = dict(result.__dict__, **changes)
    payload["run_id"] = DatasetManifest.content_digest(modified.identity_payload())
    with pytest.raises(ValidationError):
        DiscoveryRun.model_validate(payload)


def test_source_errors_keep_fixed_code_and_do_not_echo_input(tmp_path):
    ref = annotation([entity(assertions=[assertion()])])
    column = contract(namespace="synthetic-target")
    path = tmp_path / "SYN_PRIVATE.csv"
    path.write_text("synthetic_value,synthetic_feature\nSYN_PRIVATE\n")
    with pytest.raises(DiscoveryRunError, match="MALFORMED_ROW_WIDTH") as failure:
        run_discovery(path, column, annotation_reference=ref, category_condition=category_condition(ref))
    assert failure.value.row_number == 2 and "SYN_PRIVATE" not in str(failure.value)


def test_stale_category_only_source_is_rejected_before_evaluation(tmp_path, monkeypatch):
    path = write_table(tmp_path, (("1", "SYN_TARGET_A", "original"),))
    frozen = hashlib.sha256(path.read_bytes()).hexdigest()
    column = contract(namespace="synthetic-target")
    ref = annotation([entity(assertions=[assertion()])])
    kwargs = dict(annotation_reference=ref, category_condition=category_condition(ref), expected_source_sha256=frozen)
    assert run_discovery(path, column, **kwargs).observations[0].truth == "T"
    write_table(tmp_path, (("1", "SYN_TARGET_A", "replacement"),))

    def forbidden(*args, **options):
        pytest.fail("conditions were evaluated against stale source bytes")

    monkeypatch.setattr(run, "_evaluate_categories", forbidden)
    with pytest.raises(DiscoveryRunError, match="DISCOVERY_SOURCE_SHA256_MISMATCH") as failure:
        run_discovery(path, column, **kwargs)
    assert frozen not in str(failure.value)


@pytest.mark.parametrize("digest", [True, 4, "F" * 64, "f" * 63, "f" * 64 + "\n", "SYN_PRIVATE_HASH"])
def test_snapshot_digest_declaration_is_strict_and_sanitized(tmp_path, digest):
    column = contract(namespace="synthetic-target")
    ref = annotation()
    with pytest.raises(DiscoveryRunError, match="INVALID_DISCOVERY_SOURCE_SHA256") as failure:
        run_discovery(tmp_path / "not-opened.csv", column, annotation_reference=ref,
                      category_condition=category_condition(ref), expected_source_sha256=digest)
    assert "SYN_PRIVATE" not in str(failure.value)
