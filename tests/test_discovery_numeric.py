"""Exploratory comparison acceptance with entirely invented software data and declarations."""

import json
import sys
from datetime import datetime, timezone
from decimal import Inexact, Rounded, Overflow, localcontext

import pytest
from pydantic import ValidationError

from genedecision.discovery import (
    NumericCondition, NumericContext, NumericEvaluationError, NumericEvaluationResult,
    evaluate_numeric_conditions, load_numeric_condition, load_numeric_context,
    load_numeric_evaluation, replay_numeric_evaluation, verify_numeric_source,
    inspect_measurements, evaluate_categories,
)
from genedecision.discovery import numeric, tables
from genedecision.discovery.__main__ import main
from genedecision.discovery.models import DatasetManifest
from test_discovery_measurements import contract, audit_contract, table, mapping, STAMP
from test_discovery_tables import no_runtime_dependencies

pytestmark = pytest.mark.usefixtures("no_runtime_dependencies")
MID = "SYN_MEASURE_0"


def capture(tmp_path, values=("7",), *, columns=("synthetic_value",), identifiers=None, ref=None, **metadata):
    column = contract(columns=columns, mapping=ref)
    audit = audit_contract(column, **metadata)
    path = table(tmp_path, values, columns=columns, identifiers=identifiers)
    return inspect_measurements(path, column, audit, mapping_reference=ref, inspected_at=STAMP)


def predicate(pid="SYN_P0", mid=MID, operator="GE", threshold="7", **updates):
    return dict(predicate_id=pid, measurement_id=mid, operator=operator, threshold=threshold,
                provenance="Invented caller comparison", **updates)


def condition(audit, *, predicates=None, combine="ALL", context=None, **updates):
    payload = dict(measurement_audit_id=audit.audit_id, predicates=predicates or [predicate()], combine=combine,
                   context_sha256=context.content_sha256 if context else None)
    payload.update(updates)
    return NumericCondition.from_declarations(**payload)


def context(audit, statuses=(), *, scope="DATASET", locators=(), measurement_ids=(MID,), **updates):
    payload = dict(context_id="SYN_CONTEXT", release="SYN_CONTEXT_RELEASE", measurement_audit_id=audit.audit_id,
                   source_records=[dict(record_id="SYN_SOURCE_RECORD", source_id="SYN_SOURCE", release="SYN_RELEASE",
                                        source_locator="SYN_SOURCE_LOCATOR", statement="Invented unverified source")],
                   qc_declarations=[dict(declaration_id=f"SYN_QC_{i}", status=s,
                                         source_binding=dict(source_id="SYN_SOURCE", release="SYN_RELEASE"),
                                         source_record_ids=["SYN_SOURCE_RECORD"], measurement_ids=measurement_ids,
                                         scope=scope, locators=locators) for i, s in enumerate(statuses)])
    payload.update(updates)
    return NumericContext.from_declarations(**payload)


@pytest.mark.parametrize("operator,left,right,truth", [
    ("LT", "7", "8", "T"), ("LT", "7", "7", "F"),
    ("LE", "7.00", "7", "T"), ("LE", "8", "7", "F"),
    ("GT", "-7", "-8", "T"), ("GT", "7", "7", "F"),
    ("GE", "8", "7", "T"), ("GE", "7", "8", "F"),
    ("EQ", "-0.00", "+0", "T"), ("EQ", "7e-10000", "8e-10000", "F"),
    ("GT", "1e10000", "9e9999", "T"),
    ("LT", "12345678901234567890.0012300", "12345678901234567890.0012301", "T"),
])
def test_exact_ascii_decimal_operators(tmp_path, operator, left, right, truth):
    audit = capture(tmp_path, (left,))
    result = evaluate_numeric_conditions(audit, condition(audit, predicates=[predicate(operator=operator, threshold=right)]))
    row = result.rows[0]
    assert row.truth == row.predicates[0].truth == truth
    assert row.predicates[0].cell.raw_value == left
    assert row.predicates[0].qc_status == "UNKNOWN"
    assert row.scientific_validation == "NOT_PERFORMED" and row.scientific_hit_status == "NOT_CONFIRMED"


def test_precision_rounding_exponent_context_and_traps_do_not_affect_comparison(tmp_path):
    audit = capture(tmp_path, ("12345678901234567890.0012300", "1e-10000"))
    cond = condition(audit, predicates=[predicate(operator="LT", threshold="12345678901234567890.0012301")])
    with localcontext() as ctx:
        ctx.prec, ctx.Emax, ctx.Emin = 2, 2, -2
        for trap in (Inexact, Rounded, Overflow): ctx.traps[trap] = True
        low = evaluate_numeric_conditions(audit, cond)
    with localcontext() as ctx:
        ctx.prec = 100
        high = evaluate_numeric_conditions(audit, cond)
    assert low.evaluation_id == high.evaluation_id and [r.truth for r in low.rows] == ["T", "T"]


@pytest.mark.parametrize("raw", ["", "NA", "bad", "NaN", "Infinity", "1,234", "١", "9" * 1025])
def test_missing_nonfinite_invalid_limited_values_unknown_without_dropping(tmp_path, raw):
    audit = capture(tmp_path, (raw,))
    result = evaluate_numeric_conditions(audit, condition(audit))
    assert result.rows[0].truth == "U" and result.rows[0].outcome == "NUMERIC_UNKNOWN"
    assert result.rows[0].predicates[0].reason == "VALUE_UNAVAILABLE"
    assert result.rows[0].predicates[0].cell.raw_value == raw
    assert len(result.unknowns) == result.counts.input_row_count == 1


@pytest.mark.parametrize("operator,threshold", [(None, None), ("GE", None), (None, "7")])
def test_explicitly_unresolved_definition_yields_unknown(tmp_path, operator, threshold):
    audit = capture(tmp_path)
    p = predicate(operator=operator, threshold=threshold, unresolved_reason="Invented unresolved comparison")
    result = evaluate_numeric_conditions(audit, condition(audit, predicates=[p]))
    assert result.rows[0].truth == "U" and result.rows[0].predicates[0].reason == "PREDICATE_UNDEFINED"


@pytest.mark.parametrize("threshold", [True, 7, 7.0, "NaN", "Inf", " 7 ", "1_000", "7%", "١", "1e10001", "9" * 1025])
def test_thresholds_strict_finite_bounded_ascii_with_no_coercion(threshold):
    with pytest.raises(ValidationError):
        NumericCondition.from_declarations(measurement_audit_id="SYN_AUDIT", predicates=[predicate(threshold=threshold)], combine="ALL")


@pytest.mark.parametrize("combine", ["ANY", "ALL"])
@pytest.mark.parametrize("left,right", [(a,b) for a in "TFU" for b in "TFU"])
def test_three_valued_combinations_keep_individual_predicates(tmp_path, combine, left, right):
    values = {"T":"8", "F":"6", "U":"NA"}
    audit = capture(tmp_path, ((values[left], values[right]),), columns=("synthetic_value", "synthetic_other"))
    cond = condition(audit, combine=combine, predicates=[predicate(), predicate(pid="SYN_P1", mid="SYN_MEASURE_1")])
    result = evaluate_numeric_conditions(audit, cond)
    expected = ("T" if "T" in (left,right) else "U" if "U" in (left,right) else "F") if combine == "ANY" else (
        "F" if "F" in (left,right) else "U" if "U" in (left,right) else "T")
    assert result.rows[0].truth == expected
    assert [p.truth for p in result.rows[0].predicates] == [left,right]
    assert result.counts.predicate_occurrence_count == 2


@pytest.mark.parametrize("statuses,qc,flagged", [
    ((), "UNKNOWN", False), (("UNKNOWN",), "UNKNOWN", False),
    (("DECLARED_PASS",), "DECLARED_PASS", False), (("DECLARED_FAIL",), "DECLARED_FAIL", True),
    (("DECLARED_PASS", "DECLARED_FAIL"), "MIXED_DECLARATIONS", True),
    (("UNKNOWN", "DECLARED_PASS"), "MIXED_DECLARATIONS", False),
    (("DECLARED_FAIL", "DECLARED_FAIL"), "DECLARED_FAIL", True),
])
def test_qc_declarations_never_erase_numeric_truth_or_create_scientific_hit(tmp_path, statuses, qc, flagged):
    audit = capture(tmp_path, ("8", "6", "NA"))
    ctx = context(audit, statuses)
    result = evaluate_numeric_conditions(audit, condition(audit, context=ctx), context=ctx)
    assert [r.truth for r in result.rows] == ["T", "F", "U"]
    assert all(r.has_declared_qc_failure == flagged for r in result.rows)
    assert result.rows[0].predicates[0].qc_status == qc
    assert len(result.rows[0].predicates[0].qc_declaration_ids) == len(statuses)
    assert bool(result.qc_flagged_matches) == flagged
    assert all(r.warning == "SCIENTIFICALLY_UNVERIFIED" and r.scientific_status == "NOT_VALIDATED" for r in result.rows)
    assert result.rows[0].outcome == ("EXPLORATORY_NUMERIC_MATCH_QC_FLAGGED" if flagged else "EXPLORATORY_NUMERIC_MATCH")


def test_missing_context_universe_and_opaque_failure_text_not_interpreted(tmp_path):
    audit = capture(tmp_path, ("8", "6"), caller_context_text="QC FAIL / NOT_MEASURED / derived estimate")
    absent = evaluate_numeric_conditions(audit, condition(audit))
    ctx = context(audit, caller_context_text="QC FAIL", tested_universe_text="SYN_UNREPRESENTED_FEATURE")
    declared = evaluate_numeric_conditions(audit, condition(audit, context=ctx), context=ctx)
    assert [r.truth for r in absent.rows] == [r.truth for r in declared.rows] == ["T", "F"]
    assert len(declared.rows) == 2 and not any(r.has_declared_qc_failure for r in declared.rows)
    assert declared.context.tested_universe_text == "SYN_UNREPRESENTED_FEATURE"
    assert declared.evaluation_id != absent.evaluation_id


def test_exact_row_and_measurement_qc_scope_no_duplicate_id_propagation(tmp_path):
    audit = capture(tmp_path, (("8","8"), ("8","8")), columns=("synthetic_value","synthetic_other"), identifiers=("SYN_DUP","SYN_DUP"))
    ctx = context(audit, ("DECLARED_FAIL",), scope="ROWS", locators=(audit.rows[0].identifier_record.locator,))
    cond = condition(audit, context=ctx, predicates=[predicate(), predicate(pid="SYN_OTHER",mid="SYN_MEASURE_1")])
    result = evaluate_numeric_conditions(audit, cond, context=ctx)
    assert [r.truth for r in result.rows] == ["T", "T"]
    assert [r.has_declared_qc_failure for r in result.rows] == [True, False]
    assert [p.has_declared_qc_failure for p in result.rows[0].predicates] == [True, False]
    assert result.counts.raw_duplicate_row_count == 2 and len(result.exploratory_matches) == 2


def test_ambiguous_and_missing_mapping_or_identifier_never_resolve_but_numeric_stays_visible(tmp_path):
    reference = mapping()
    audit = capture(tmp_path, ("8","8","6","8"), identifiers=("SYN_B","SYN_B","SYN_UNMAPPED",""), ref=reference)
    result = evaluate_numeric_conditions(audit, condition(audit))
    assert [r.truth for r in result.rows] == ["T","T","F","T"]
    assert [r.identifier_record for r in result.rows] == [r.identifier_record for r in audit.rows]
    assert result.rows[0].identifier_record.mapping_targets == ("SYN_TARGET_B1","SYN_TARGET_B2")
    assert result.rows[0].identifier_record.status == "UNRESOLVED_AMBIGUOUS"
    assert result.counts.ambiguous_identity_row_count == 2
    with pytest.raises(NumericEvaluationError, match="NUMERIC_MAPPING_REFERENCE_REQUIRED"):
        replay_numeric_evaluation(result, measurement_audit=audit, condition=result.condition)
    assert replay_numeric_evaluation(result, measurement_audit=audit, condition=result.condition, mapping_reference=reference) == result
    assert verify_numeric_source(tmp_path / "synthetic.csv", result, mapping_reference=reference).evaluation_id == result.evaluation_id


def test_category_truth_and_audit_schemas_remain_unchanged(tmp_path):
    from test_discovery_categories import annotation, entity, assertion, condition as category_condition
    from test_discovery_measurements import contract as col_contract
    column = col_contract(namespace="synthetic-target")
    audit = inspect_measurements(table(tmp_path, ("8",), identifiers=("SYN_TARGET_A",)), column, audit_contract(column), inspected_at=STAMP)
    ref = annotation([entity(assertions=[assertion()])])
    cond = category_condition(ref)
    before = evaluate_categories(audit.identifiers, annotation_reference=ref, conditions=cond)
    ctx = context(audit, ("DECLARED_FAIL",))
    numeric_result = evaluate_numeric_conditions(audit, condition(audit, context=ctx), context=ctx)
    after = evaluate_categories(numeric_result.measurement_audit.identifiers, annotation_reference=ref, conditions=cond)
    assert before.evaluation_id == after.evaluation_id and after.rows[0].truth == "T"
    assert numeric_result.rows[0].outcome == "EXPLORATORY_NUMERIC_MATCH_QC_FLAGGED"
    assert numeric_result.measurement_audit == audit


@pytest.mark.parametrize("updates", [dict(combine="NOT"), dict(combine="AUTO"), dict(predicates=[]),
    dict(predicates=[predicate()] * 2), dict(default_threshold="SYN_PRIVATE"), dict(scientific_validation="VALIDATED"),
    dict(predicates=[predicate(operator=None)]), dict(predicates=[predicate(unresolved_reason="SYN_INCONSISTENT")])])
def test_condition_strict_structure_and_no_defaults(tmp_path, updates):
    audit = capture(tmp_path)
    payload = condition(audit).identity_payload()
    payload.update(updates)
    with pytest.raises(ValidationError): NumericCondition.from_declarations(**payload)


@pytest.mark.parametrize("fault", ["source_missing", "source_release", "rows_empty", "dataset_rows", "duplicate_ids", "bad_status"])
def test_qc_structure_sources_and_version_chains_are_checked(tmp_path, fault):
    audit = capture(tmp_path)
    payload = context(audit, ("DECLARED_FAIL",)).identity_payload()
    declaration = payload["qc_declarations"][0]
    if fault == "source_missing": declaration["source_record_ids"] = ["SYN_MISSING"]
    if fault == "source_release": declaration["source_binding"]["release"] = "SYN_OTHER_RELEASE"
    if fault == "rows_empty": declaration["scope"] = "ROWS"
    if fault == "dataset_rows": declaration["locators"] = [audit.rows[0].identifier_record.locator.model_dump()]
    if fault == "duplicate_ids": payload["qc_declarations"].append(declaration.copy())
    if fault == "bad_status": declaration["status"] = "VERIFIED_PASS"
    with pytest.raises(ValidationError): NumericContext.from_declarations(**payload)


@pytest.mark.parametrize("fault,code", [("audit","NUMERIC_AUDIT_BINDING_MISMATCH"),
    ("measurement","NUMERIC_MEASUREMENT_NOT_SELECTED"), ("context","NUMERIC_CONTEXT_BINDING_MISMATCH"),
    ("context_audit","NUMERIC_CONTEXT_AUDIT_MISMATCH"), ("qc_measurement","NUMERIC_QC_MEASUREMENT_NOT_SELECTED"),
    ("locator","NUMERIC_QC_LOCATOR_NOT_PRESENT")])
def test_binding_and_scope_configuration_failures_are_fixed(tmp_path, fault, code):
    audit = capture(tmp_path)
    ctx = context(audit)
    kwargs = {}
    if fault == "audit": kwargs["measurement_audit_id"] = "SYN_OTHER"
    if fault == "measurement": kwargs["predicates"] = [predicate(mid="SYN_UNSELECTED")]
    if fault == "context": kwargs["context_sha256"] = "0" * 64
    if fault == "context_audit":
        ctx = context(audit, measurement_audit_id="SYN_OTHER")
    if fault == "qc_measurement": ctx = context(audit, ("DECLARED_FAIL",), measurement_ids=("SYN_UNSELECTED",))
    if fault == "locator": ctx = context(audit, ("DECLARED_FAIL",), scope="ROWS", locators=[dict(logical_record=9,physical_line_end=10)])
    cond = condition(audit, context=ctx, **kwargs)
    with pytest.raises(NumericEvaluationError) as exc: evaluate_numeric_conditions(audit, cond, context=ctx)
    assert exc.value.code == code


@pytest.mark.parametrize("loader,kind", [(load_numeric_condition,"condition"),(load_numeric_context,"context"),(load_numeric_evaluation,"result")])
@pytest.mark.parametrize("mutation", ["duplicate_top","duplicate_nested","nan","extra","digest"])
def test_public_json_boundaries_reject_private_corruption_without_echo(tmp_path, loader, kind, mutation):
    audit = capture(tmp_path)
    ctx = context(audit, ("DECLARED_FAIL",))
    cond = condition(audit, context=ctx)
    obj = {"condition":cond,"context":ctx,"result":evaluate_numeric_conditions(audit,cond,context=ctx)}[kind]
    text = obj.model_dump_json()
    if mutation == "duplicate_top": text = '{"schema_version":"SYN_PRIVATE",' + text[1:]
    if mutation == "duplicate_nested":
        key = '"predicate_id":"SYN_P0"' if kind != "context" else '"record_id":"SYN_SOURCE_RECORD"'
        text = text.replace(key, key + ',' + key, 1)
    if mutation == "nan": text = '{"SYN_PRIVATE":NaN}'
    if mutation == "extra": text = '{"SYN_PRIVATE":"SYN_PRIVATE",' + text[1:]
    if mutation == "digest": text = text.replace(obj.evaluation_id if kind == "result" else obj.content_sha256, "0" * 64, 1)
    with pytest.raises(NumericEvaluationError) as exc: loader(text.encode())
    assert "SYN_PRIVATE" not in str(exc.value)


def test_rehashed_wrong_truth_is_rejected_and_scientific_promotion_forbidden(tmp_path):
    audit = capture(tmp_path, ("8",))
    result = evaluate_numeric_conditions(audit, condition(audit))
    payload = result.model_dump(mode="json")
    payload["rows"][0]["truth"] = "F"
    payload["rows"][0]["outcome"] = "EXPLORATORY_NUMERIC_NONMATCH"
    payload["rows"][0]["predicates"][0].update(truth="F", reason="COMPARISON_FAILED")
    row = numeric.NumericRowResult(**payload["rows"][0])
    payload["counts"] = numeric._counts((row,)).model_dump(mode="json")
    forged = result.model_copy(update={"rows":(row,),"counts":numeric._counts((row,))})
    payload["evaluation_id"] = "NUMERIC_" + DatasetManifest.content_digest(forged.identity_payload())
    with pytest.raises(NumericEvaluationError, match="INVALID_NUMERIC_EVALUATION"):
        load_numeric_evaluation(json.dumps(payload))
    for field,value in (("scientific_validation","PERFORMED"),("scientific_hit_status","CONFIRMED"),("warning","")):
        invalid = result.model_dump(mode="json")
        invalid[field] = value
        with pytest.raises(NumericEvaluationError): load_numeric_evaluation(json.dumps(invalid))


@pytest.mark.parametrize("invalid", [True, 1.0])
def test_nested_row_locator_copy_and_qc_locator_copy_are_strict_even_rehashed(tmp_path, invalid):
    audit = capture(tmp_path)
    ctx = context(audit, ("DECLARED_FAIL",), scope="ROWS", locators=(audit.rows[0].identifier_record.locator,))
    result = evaluate_numeric_conditions(audit, condition(audit, context=ctx), context=ctx)
    record = result.rows[0].identifier_record
    bad_locator = record.locator.model_copy(update={"logical_record":invalid})
    bad_row = result.rows[0].model_copy(update={"identifier_record":record.model_copy(update={"locator":bad_locator})})
    identity = result.identity_payload()
    identity["rows"][0]["identifier_record"]["locator"]["logical_record"] = invalid
    payload = result.model_dump(mode="python")
    payload.update(rows=(bad_row,), evaluation_id="NUMERIC_" + DatasetManifest.content_digest(identity))
    with pytest.raises(ValidationError): NumericEvaluationResult.model_validate(payload)
    bad_declaration = ctx.qc_declarations[0].model_copy(update={"locators":(bad_locator,)})
    mixed = ctx.model_dump(mode="python")
    mixed["qc_declarations"] = (bad_declaration,)
    with pytest.raises(ValidationError): NumericContext.model_validate(mixed)


def test_valid_mixed_models_serialization_and_reference_replay(tmp_path):
    audit = capture(tmp_path, ("8","NA"))
    ctx = context(audit,("DECLARED_PASS",))
    cond = condition(audit, context=ctx)
    mixed = audit.model_dump(mode="python")
    mixed.update(rows=audit.rows, identifiers=audit.identifiers)
    result = evaluate_numeric_conditions(mixed, cond.model_dump(mode="python"), context=ctx)
    loaded = load_numeric_evaluation(result.model_dump_json())
    assert loaded == result
    assert replay_numeric_evaluation(loaded, measurement_audit=audit, condition=cond, context=ctx) == result
    assert verify_numeric_source(tmp_path / "synthetic.csv", result).evaluation_id == result.evaluation_id
    changed_context = context(audit,("DECLARED_FAIL",))
    with pytest.raises(NumericEvaluationError, match="NUMERIC_REFERENCE_REPLAY_MISMATCH"):
        replay_numeric_evaluation(result,measurement_audit=audit,condition=condition(audit,context=changed_context),context=changed_context)
    assert NumericEvaluationResult.model_json_schema()["additionalProperties"] is False


def test_source_changed_after_snapshot_and_before_fresh_verification(tmp_path,monkeypatch):
    column = contract()
    ac = audit_contract(column)
    path = table(tmp_path,("8",))
    original = tables.read_evidence_rows
    def replace(snapshot,**kwargs):
        path.write_text("synthetic_feature,synthetic_value\nSYN_CHANGED,6\n")
        return original(snapshot,**kwargs)
    monkeypatch.setattr(tables,"read_evidence_rows",replace)
    audit = inspect_measurements(path,column,ac)
    result = evaluate_numeric_conditions(audit,condition(audit))
    assert result.rows[0].truth == "T" and result.rows[0].predicates[0].cell.raw_value == "8"
    monkeypatch.setattr(tables,"read_evidence_rows",original)
    with pytest.raises(tables.DatasetInspectionError,match="MEASUREMENT_SOURCE_REPLAY_MISMATCH"):
        verify_numeric_source(path,result)


def test_determinism_times_paths_and_original_row_order(tmp_path):
    first = capture(tmp_path,("8","6","NA"))
    column = first.identifiers.column_contract
    second = inspect_measurements(table(tmp_path,("8","6","NA"),name="renamed.csv"), column,first.audit_contract,
                                  inspected_at=datetime(2026,2,1,tzinfo=timezone.utc))
    assert first.audit_id == second.audit_id
    cond = condition(first)
    a,b = evaluate_numeric_conditions(first,cond),evaluate_numeric_conditions(second,cond)
    assert a.evaluation_id == b.evaluation_id
    assert [r.identifier_record.locator.logical_record for r in a.rows] == [1,2,3]
    assert a.counts.row_truth_counts == dict(T=1,F=1,U=1)


def test_evaluation_limit_checked_before_materializing_results(tmp_path,monkeypatch):
    audit = capture(tmp_path,("8","8"))
    cond = condition(audit,predicates=[predicate(),predicate(pid="SYN_P1")])
    def forbidden(*args,**kwargs): raise AssertionError("results built past cap")
    monkeypatch.setattr(numeric,"_evaluate_rows",forbidden)
    with pytest.raises(NumericEvaluationError,match="NUMERIC_EVALUATION_LIMIT"):
        evaluate_numeric_conditions(audit,cond,max_evaluations=3)
    for invalid in (True,0,"1",1.0):
        with pytest.raises(NumericEvaluationError,match="INVALID_NUMERIC_EVALUATION_LIMIT"):
            evaluate_numeric_conditions(audit,cond,max_evaluations=invalid)


def cli_files(tmp_path, values=("8","6","NA")):
    audit = capture(tmp_path,values)
    ctx = context(audit,("DECLARED_FAIL",),caller_context_text="SYN_PRIVATE_CONTEXT")
    cond = condition(audit,context=ctx)
    paths=[]
    for name,obj in (("column",audit.identifiers.column_contract),("audit",audit.audit_contract),("condition",cond),("context",ctx)):
        path=tmp_path/("synthetic_"+name+".json")
        path.write_text(obj.model_dump_json())
        paths.append(path)
    return paths,cond,ctx


@pytest.mark.parametrize("json_mode",[False,True])
def test_numeric_cli_only_fixed_counts_and_unverified_labels(tmp_path,monkeypatch,capsys,json_mode):
    (column,audit,cond_path,ctx_path),cond,ctx=cli_files(tmp_path)
    argv=["SYN_PRIVATE_PROGRAM",str(tmp_path/"synthetic.csv"),"--column-contract",str(column),
          "--measurement-audit-contract",str(audit),"--numeric-condition",str(cond_path),"--numeric-context",str(ctx_path)]
    monkeypatch.setattr(sys,"argv",argv+(["--json"] if json_mode else []))
    assert main()==0
    output=capsys.readouterr()
    assert not output.err
    for private in (str(tmp_path),"SYN_PRIVATE","synthetic_feature","synthetic_value",cond.content_sha256,ctx.content_sha256):
        assert private not in output.out
    assert "SCIENTIFICALLY_UNVERIFIED" in output.out
    if json_mode:
        summary=json.loads(output.out)
        assert summary["row_truth_counts"]==dict(T=1,F=1,U=1) and summary["qc_flagged_match_count"]==1


@pytest.mark.parametrize("fault",["no_audit","no_column","context_only","category","bad_context","bad_condition","missing_context"])
def test_cli_errors_and_conflicts_safe(tmp_path,monkeypatch,capsys,fault):
    (column,audit,cond,ctx),_,_=cli_files(tmp_path)
    argv=["SYN_PRIVATE_PROGRAM",str(tmp_path/"synthetic.csv")]
    if fault!="no_column": argv += ["--column-contract",str(column)]
    if fault!="no_audit": argv += ["--measurement-audit-contract",str(audit)]
    if fault!="context_only": argv += ["--numeric-condition",str(cond)]
    argv += ["--numeric-context",str(ctx)]
    if fault=="category": argv += ["--category-condition","SYN_PRIVATE_PATH"]
    if fault=="bad_context": ctx.write_text('{"SYN_PRIVATE":"SYN_PRIVATE"}')
    if fault=="bad_condition": cond.write_text('{"SYN_PRIVATE":"SYN_PRIVATE"}')
    if fault=="missing_context": ctx.unlink()
    monkeypatch.setattr(sys,"argv",argv)
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code==2
    output=capsys.readouterr()
    assert not output.out and "SYN_PRIVATE" not in output.err and str(tmp_path) not in output.err


@pytest.mark.parametrize("raw,outcome",[("6","EXPLORATORY_NUMERIC_NONMATCH"),("NA","NUMERIC_UNKNOWN")])
def test_text_cli_shows_failed_qc_even_without_numeric_match(tmp_path,monkeypatch,capsys,raw,outcome):
    (column,audit,cond,ctx),_,_=cli_files(tmp_path,values=(raw,))
    monkeypatch.setattr(sys,"argv",["numeric",str(tmp_path/"synthetic.csv"),"--column-contract",str(column),
        "--measurement-audit-contract",str(audit),"--numeric-condition",str(cond),"--numeric-context",str(ctx)])
    assert main()==0
    output=capsys.readouterr().out
    assert f"{outcome}: 1" in output
    assert "QC_FAILURE_DECLARED_ROWS: 1" in output and "QC_DECLARED_FAIL: 1" in output
    assert "SYN_PRIVATE" not in output


def test_complete_public_demo_runs_in_guarded_environment_and_cleans_files(capsys):
    import runpy
    from pathlib import Path
    demo=Path(__file__).resolve().parents[1]/"examples/discovery_numeric_demo.py"
    with pytest.raises(SystemExit) as exc: runpy.run_path(str(demo),run_name="__main__")
    assert exc.value.code==0
    output=capsys.readouterr().out
    assert "SYNTHETIC_NUMERIC_DEMO_OK" in output and "SCIENTIFICALLY_UNVERIFIED" in output
    assert "DECLARED_FAIL" in output and "UNRESOLVED_AMBIGUOUS" in output


def test_zero_matches_is_valid_and_all_rows_are_still_available(tmp_path):
    audit=capture(tmp_path,("6","NA","6"),identifiers=("SYN_DUP","SYN_OTHER","SYN_DUP"))
    result=evaluate_numeric_conditions(audit,condition(audit))
    assert not result.exploratory_matches and len(result.nonmatches)==2 and len(result.unknowns)==1
    assert result.counts.row_truth_counts==dict(T=0,F=2,U=1)
    assert len(result.rows)==3 and result.counts.raw_duplicate_row_count==2


def test_cli_reads_each_declaration_and_source_once(tmp_path,monkeypatch,capsys):
    from pathlib import Path
    (column,audit,cond,ctx),_,_=cli_files(tmp_path)
    source=tmp_path/"synthetic.csv"
    original=Path.open
    reads=[]
    def count(self,*args,**kwargs):
        if self in (column,audit,cond,ctx,source): reads.append(self)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,"open",count)
    monkeypatch.setattr(sys,"argv",["numeric",str(source),"--column-contract",str(column),
        "--measurement-audit-contract",str(audit),"--numeric-condition",str(cond),"--numeric-context",str(ctx)])
    assert main()==0 and reads==[column,audit,cond,ctx,source]
    assert "SYN_PRIVATE" not in capsys.readouterr().out
