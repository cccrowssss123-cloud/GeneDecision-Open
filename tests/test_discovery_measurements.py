"""Invented software inputs only; lexical audit never establishes scientific truth."""

import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Inexact, Rounded, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from genedecision.discovery import (
    ColumnContract, LocalMappingReference, MappingEntry, MeasurementAuditContract,
    MeasurementAuditError, MeasurementAuditResult,
    evaluate_categories, inspect_identifiers, inspect_measurements,
    load_measurement_audit_contract, load_measurement_audit_result, verify_measurement_source,
)
from genedecision.discovery import measurements, tables
from genedecision.discovery.__main__ import main
from genedecision.discovery.models import DatasetManifest
from test_discovery_tables import no_runtime_dependencies

pytestmark = pytest.mark.usefixtures("no_runtime_dependencies")
STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def contract(*, columns=("synthetic_value",), mapping=None, **updates):
    payload = dict(primary_gene=dict(column="synthetic_feature", definition="Invented identifier"),
                   measurements=[dict(column=c, definition="Invented value declaration") for c in columns],
                   namespace="synthetic-source", taxon="synthetic-taxon",
                   mapping_reference=mapping.binding if mapping else None)
    payload.update(updates)
    return ColumnContract(**payload)


def audit_contract(column, *, policies=None, **updates):
    if policies is None:
        policies = [dict(measurement_id=f"SYN_MEASURE_{i}", column=m.column) for i, m in enumerate(column.measurements)]
    return MeasurementAuditContract.from_declarations(
        column_contract_sha256=column.content_sha256, policies=policies, **updates)


def table(tmp_path, values=("7",), *, identifiers=None, columns=("synthetic_value",),
          name="synthetic.csv", encoding="utf-8", delimiter=","):
    path = tmp_path / name
    identifiers = identifiers or tuple(f"SYN_FEATURE_{i}" for i in range(len(values)))
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(["synthetic_feature", *columns])
        for raw, value in zip(identifiers, values):
            writer.writerow([raw, *(value if isinstance(value, tuple) else (value,))])
    return path


def inspect(tmp_path, values, *, strip=False, tokens=(), **declarations):
    column = contract()
    audit = audit_contract(column, policies=[dict(measurement_id="SYN_MEASURE", column="synthetic_value",
                                                strip_ascii_whitespace=strip, missing_tokens=tokens)], **declarations)
    return inspect_measurements(table(tmp_path, values), column, audit, inspected_at=STAMP)


@pytest.mark.parametrize("raw,sign,digits,exponent", [
    ("0", 0, (0,), 0), ("-0", 1, (0,), 0), ("-0.00", 1, (0,), -2),
    ("0007", 0, (7,), 0), ("+7.00", 0, (7, 0, 0), -2),
    (".25", 0, (2, 5), -2), ("7.", 0, (7,), 0),
    ("-12.34e+5", 1, (1, 2, 3, 4), 3), ("1e-10000", 0, (1,), -10000),
    ("1E10000", 0, (1,), 10000), ("9e000000001", 0, (9,), 1),
])
def test_exact_finite_tuple_preserves_sign_and_scale_without_float(tmp_path, raw, sign, digits, exponent):
    cell = inspect(tmp_path, (raw,)).rows[0].cells[0]
    assert cell.status == "FINITE_DECIMAL" and cell.raw_value == cell.parse_input == raw
    assert cell.exact_decimal.model_dump() == dict(sign=sign, digits=digits, exponent=exponent)


@pytest.mark.parametrize("raw,status", [
    ("", "BLANK"), (" \t\n\r\v\f", "BLANK"),
    ("NA", "NON_NUMERIC"), ("Na", "NON_NUMERIC"), ("null", "NON_NUMERIC"),
    ("1,234", "NON_NUMERIC"), ("7%", "NON_NUMERIC"), ("1_000", "NON_NUMERIC"),
    ("0x10", "NON_NUMERIC"), ("1/2", "NON_NUMERIC"), ("2026-01-01", "NON_NUMERIC"),
    ("١٢", "NON_NUMERIC"), ("１２", "NON_NUMERIC"), ("\u00a0", "NON_NUMERIC"),
    ("nan", "NONFINITE_TOKEN"), ("-NaN", "NONFINITE_TOKEN"), ("sNaN123", "NONFINITE_TOKEN"),
    ("+INF", "NONFINITE_TOKEN"), ("-Infinity", "NONFINITE_TOKEN"),
    (" 7 ", "NON_NUMERIC"), ("1\n", "NON_NUMERIC"),
])
def test_nonvalues_are_distinct_lexical_observations_not_scientific_missingness(tmp_path, raw, status):
    result = inspect(tmp_path, (raw,))
    cell = result.rows[0].cells[0]
    assert cell.raw_value == raw and cell.status == status and cell.exact_decimal is None
    assert result.counts.cell_count == 1 and sum(result.counts.cell_status_counts.values()) == 1
    assert not {"truth", "outcome", "eligibility", "qc", "scientific_missingness"}.intersection(type(cell).model_fields)


def test_missing_tokens_are_raw_exact_before_ascii_strip_and_zero_is_preserved(tmp_path):
    values = ("NA", "na", " NA ", "0", "-0", " 7 ", "\u00a07\u00a0", " NAN ")
    result = inspect(tmp_path, values, strip=True, tokens=("NA", "0"))
    assert [r.cells[0].status for r in result.rows] == ["DECLARED_MISSING_TOKEN", "NON_NUMERIC", "NON_NUMERIC",
        "DECLARED_MISSING_TOKEN", "FINITE_DECIMAL", "FINITE_DECIMAL", "NON_NUMERIC", "NONFINITE_TOKEN"]
    assert result.rows[2].cells[0].parse_input == "NA" and result.rows[2].cells[0].raw_value == " NA "
    assert result.rows[4].cells[0].exact_decimal.sign == 1
    assert inspect(tmp_path, ("0",)).rows[0].cells[0].status == "FINITE_DECIMAL"


@pytest.mark.parametrize("raw,status", [
    ("9" * 1024, "FINITE_DECIMAL"), ("9" * 1025, "PARSER_LIMIT"),
    ("0" * 4096, "FINITE_DECIMAL"), ("0" * 4097, "PARSER_LIMIT"),
    (" " * 4097, "PARSER_LIMIT"), ("1e10001", "PARSER_LIMIT"),
    ("1e-10001", "PARSER_LIMIT"), (".1e-10000", "PARSER_LIMIT"),
    ("1e" + "9" * 1000, "PARSER_LIMIT"),
    ("1e" + "0" * 1000 + "2", "FINITE_DECIMAL"),
])
def test_engineering_limits_never_round_truncate_fill_zero_or_drop_rows(tmp_path, raw, status):
    result = inspect(tmp_path, (raw,))
    assert result.rows[0].cells[0].raw_value == raw and result.rows[0].cells[0].status == status
    assert result.counts.input_row_count == result.counts.cell_count == 1


def test_decimal_precision_and_traps_cannot_round_the_capture(tmp_path):
    raw = "12345678901234567890.0012300"
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        low = inspect(tmp_path, (raw,))
    with localcontext() as ctx:
        ctx.prec = 80
        high = inspect(tmp_path, (raw,))
    assert low.audit_id == high.audit_id
    assert low.rows[0].cells[0].exact_decimal.digits == tuple(int(c) for c in "123456789012345678900012300")
    assert low.rows[0].cells[0].exact_decimal.exponent == -7


def test_multi_column_counts_policy_order_duplicates_and_multiline_locators(tmp_path):
    columns = ("synthetic_value", "synthetic_other")
    column = contract(columns=columns)
    audit = audit_contract(column, policies=[dict(measurement_id="SYN_B", column=columns[1], missing_tokens=["SYN_NA"]),
                                            dict(measurement_id="SYN_A", column=columns[0], strip_ascii_whitespace=True)])
    path = table(tmp_path, ((" 7 ", "SYN_NA"), ("8", "bad\ntext"), ("9", "0")),
                 identifiers=("SYN_DUP", "SYN_DUP", "SYN_OTHER"), columns=columns)
    result = inspect_measurements(path, column, audit, inspected_at=STAMP)
    assert [c.column for c in result.rows[0].cells] == [columns[1], columns[0]]
    assert result.rows[1].cells[0].raw_value == "bad\ntext"
    assert result.rows[1].identifier_record.locator.physical_line_end == 4
    assert result.counts.input_row_count == 3 and result.counts.measurement_column_count == 2 and result.counts.cell_count == 6
    assert result.counts.cell_status_counts == dict(BLANK=0, DECLARED_MISSING_TOKEN=1, FINITE_DECIMAL=4,
                                                 NONFINITE_TOKEN=0, NON_NUMERIC=1, PARSER_LIMIT=0)
    assert result.counts.raw_duplicate_row_count == 2
    assert tuple(row.identifier_record for row in result.rows) == result.identifiers.identifiers


def mapping():
    return LocalMappingReference.from_entries(reference_id="SYN_MAPPING", source_namespace="synthetic-source",
        target_namespace="synthetic-target", taxon="synthetic-taxon", entries=tuple(MappingEntry(source=s, target=t) for s, t in (
            ("SYN_A", "SYN_TARGET_A"), ("SYN_A", "SYN_TARGET_A"),
            ("SYN_B", "SYN_TARGET_B1"), ("SYN_B", "SYN_TARGET_B2"))))


@pytest.mark.parametrize("reference_supplied", [False, True])
def test_shared_builder_preserves_full_6b2_identity_and_all_mapping_targets(tmp_path, reference_supplied):
    reference = mapping()
    column = contract(mapping=reference, lexical_policy=dict(strip_whitespace=True, case="UPPER"), missing_identifier_tokens=["SYN_MISSING"])
    path = table(tmp_path, ("1", "2", "3", "4", "5", "6"), identifiers=(" syn_a ", "SYN_B", "SYN_C", "SYN_MISSING", "", "SYN_B"))
    supplied = reference if reference_supplied else None
    legacy = inspect_identifiers(path, column, mapping_reference=supplied, inspected_at=STAMP)
    result = inspect_measurements(path, column, audit_contract(column), mapping_reference=supplied, inspected_at=STAMP)
    assert result.identifiers == legacy
    assert result.identifiers.processing_id == legacy.processing_id and result.input_binding.processing_id == legacy.processing_id
    assert result.identifiers.inspection.manifest == legacy.inspection.manifest
    if reference_supplied:
        assert result.rows[0].identifier_record.mapping_entry_count == 2
        assert result.rows[1].identifier_record.mapping_targets == ("SYN_TARGET_B1", "SYN_TARGET_B2")
        assert result.counts.ambiguous_row_count == 2
    else:
        assert result.rows[0].identifier_record.status == "UNRESOLVED_REFERENCE_NOT_SUPPLIED"
    assert verify_measurement_source(path, result, mapping_reference=supplied).audit_id == result.audit_id


def test_context_and_source_text_remain_verbatim_not_qc_universe_domain_or_truth(tmp_path):
    context = " QC FAIL; NOT_MEASURED; estimate; tested universe unknown \n"
    source = dict(record_id="SYN_SOURCE", source_locator=" SYN_LOCATOR \n", text="NOT_MEASURED + DERIVED  ")
    result = inspect(tmp_path, ("-7.25", "9000"), caller_context_text=context, source_declarations=[source])
    assert result.audit_contract.caller_context_text == context
    assert result.audit_contract.source_declarations[0].model_dump() == source
    assert all(r.cells[0].status == "FINITE_DECIMAL" for r in result.rows)
    assert not {"truth", "outcome", "qc", "eligibility", "tested_universe", "conflicts", "confirmed_matches"}.intersection(type(result).model_fields)
    assert load_measurement_audit_result(result.model_dump_json()) == result


def test_audit_does_not_change_existing_category_truth(tmp_path):
    from test_discovery_categories import annotation, condition, entity, assertion
    column = contract(namespace="synthetic-target")
    path = table(tmp_path, ("-999",), identifiers=("SYN_TARGET_A",))
    before = inspect_identifiers(path, column, inspected_at=STAMP)
    capture = inspect_measurements(path, column, audit_contract(column, caller_context_text="QC FAIL / NOT_MEASURED"), inspected_at=STAMP)
    ref = annotation([entity(assertions=[assertion()])])
    cond = condition(ref)
    prior = evaluate_categories(before, annotation_reference=ref, conditions=cond)
    after = evaluate_categories(capture.identifiers, annotation_reference=ref, conditions=cond)
    assert prior.evaluation_id == after.evaluation_id and after.rows[0].truth == "T"


@pytest.mark.parametrize("updates", [
    dict(policies=[]), dict(policies=[dict(measurement_id="SYN_X", column="synthetic_value")] * 2),
    dict(policies=[dict(measurement_id=" ", column="synthetic_value")]),
    dict(policies=[dict(measurement_id="SYN_X", column="synthetic_value", missing_tokens=["NA", "NA"])]),
    dict(policies=[dict(measurement_id="SYN_X", column="synthetic_value", missing_tokens=["  "])]),
    dict(policies=[dict(measurement_id="SYN_X", column="synthetic_value", missing_tokens=[1])]),
    dict(policies=[dict(measurement_id="SYN_X", column="synthetic_value", strip_ascii_whitespace="true")]),
    dict(policies=[dict(measurement_id="SYN_X", column="synthetic_value", missing_token_matching="CASEFOLD")]),
    dict(parser_semantics="AUTO"), dict(parser_limits=dict(significant_digits=10)),
    dict(parser_limits=dict(exponent_magnitude="10000")), dict(parser_limits=dict(token_characters=4096.0)),
    dict(qc_required=True), dict(numeric_condition="SYN_PRIVATE"),
])
def test_strict_declarations_reject_unsupported_semantics_and_types(updates):
    column = contract()
    payload = audit_contract(column).identity_payload()
    payload.update(updates)
    with pytest.raises(ValidationError):
        MeasurementAuditContract.from_declarations(**payload)


@pytest.mark.parametrize("columns", [(), ("synthetic_value",), ("synthetic_value", "synthetic_other", "SYN_UNSELECTED")])
def test_audit_policies_must_cover_every_selected_column_exactly(tmp_path, columns):
    column = contract(columns=("synthetic_value", "synthetic_other"))
    path = table(tmp_path, (("1", "2"),), columns=("synthetic_value", "synthetic_other"))
    if not columns:
        with pytest.raises(ValidationError): audit_contract(column, policies=[])
    else:
        audit = audit_contract(column, policies=[dict(measurement_id=f"SYN_{i}", column=c) for i, c in enumerate(columns)])
        with pytest.raises(MeasurementAuditError, match="MEASUREMENT_POLICY_COVERAGE_MISMATCH"):
            inspect_measurements(path, column, audit)


def test_column_digest_binding_and_mapping_reference_mismatch_fail_before_read(tmp_path, monkeypatch):
    column = contract()
    audit = audit_contract(column)
    def forbidden(*args, **kwargs): raise AssertionError("invalid declaration read source")
    monkeypatch.setattr(measurements, "_inspect_table_with_rows", forbidden)
    changed = contract(namespace="synthetic-other")
    with pytest.raises(MeasurementAuditError, match="MEASUREMENT_COLUMN_BINDING_MISMATCH"):
        inspect_measurements(tmp_path / "SYN_PRIVATE_PATH", changed, audit)
    with pytest.raises(MeasurementAuditError, match="MAPPING_REFERENCE_BINDING_MISMATCH"):
        inspect_measurements(tmp_path / "SYN_PRIVATE_PATH", column, audit, mapping_reference=mapping())


@pytest.mark.parametrize("mutation", ["top_duplicate", "nested_duplicate", "unknown", "nan", "digest", "utf8"])
def test_audit_loader_rejects_duplicate_json_and_bad_content_with_fixed_errors(mutation):
    declaration = audit_contract(contract())
    text = declaration.model_dump_json()
    if mutation == "top_duplicate": text = '{"content_sha256":"SYN_PRIVATE",' + text[1:]
    if mutation == "nested_duplicate": text = text.replace('"measurement_id":"SYN_MEASURE_0"', '"measurement_id":"SYN_PRIVATE","measurement_id":"SYN_MEASURE_0"')
    if mutation == "unknown": text = '{"SYN_PRIVATE":"SYN_PRIVATE",' + text[1:]
    if mutation == "nan": text = '{"SYN_PRIVATE":NaN}'
    if mutation == "digest": text = text.replace(declaration.content_sha256, "0" * 64)
    if mutation == "utf8": text = b"\xff"
    with pytest.raises(MeasurementAuditError) as exc: load_measurement_audit_contract(text)
    assert str(exc.value) == "INVALID_MEASUREMENT_AUDIT_CONTRACT: local measurement audit failed"


def test_source_replacement_after_snapshot_cannot_mix_identifiers_and_measurements(tmp_path, monkeypatch):
    path = table(tmp_path, ("0007.2500",), identifiers=("SYN_ORIGINAL",))
    raw = path.read_bytes()
    original = tables.read_evidence_rows
    calls = []
    def replace(snapshot, **kwargs):
        calls.append(Path(snapshot))
        assert Path(snapshot) != path
        path.write_text("synthetic_feature,synthetic_value\nSYN_REPLACED,999\n")
        return original(snapshot, **kwargs)
    monkeypatch.setattr(tables, "read_evidence_rows", replace)
    column = contract()
    result = inspect_measurements(path, column, audit_contract(column))
    assert result.rows[0].identifier_record.raw_identifier == "SYN_ORIGINAL"
    assert result.rows[0].cells[0].raw_value == "0007.2500"
    assert result.input_binding.source_file_sha256 == hashlib.sha256(raw).hexdigest()
    assert len(calls) == 1 and not calls[0].exists()


def test_real_table_read_once_and_no_source_reopen_for_cells(tmp_path, monkeypatch):
    path = table(tmp_path, ("1", "2"))
    original = Path.open
    calls = []
    def count_open(self, *args, **kwargs):
        if self == path: calls.append(args)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", count_open)
    column = contract()
    inspect_measurements(path, column, audit_contract(column))
    assert calls == [("rb",)]


def test_determinism_path_time_and_declarations_are_bound(tmp_path):
    column = contract()
    audit = audit_contract(column)
    first = inspect_measurements(table(tmp_path), column, audit, inspected_at=STAMP)
    second = inspect_measurements(table(tmp_path, name="synthetic-renamed.csv"), column, audit,
                                  inspected_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert first.audit_id == second.audit_id
    changed = inspect_measurements(table(tmp_path), column, audit_contract(column, caller_context_text="SYN_DIFFERENT"), inspected_at=STAMP)
    assert changed.audit_id != first.audit_id and changed.input_binding.processing_id == first.input_binding.processing_id
    assert load_measurement_audit_contract(audit.model_dump_json()) == audit
    assert MeasurementAuditResult.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize("field", ["status", "raw", "parse_input", "decimal", "locator", "counts", "digest", "binding", "classification"])
def test_result_revalidates_parse_raw_relationships_locators_counts_and_digests(tmp_path, field):
    result = inspect(tmp_path, ("7.00",))
    payload = result.model_dump(mode="json")
    cell = payload["rows"][0]["cells"][0]
    if field == "status": cell["status"] = "NON_NUMERIC"
    if field == "raw": cell["raw_value"] = "SYN_PRIVATE"
    if field == "parse_input": cell["parse_input"] = "SYN_PRIVATE"
    if field == "decimal": cell["exact_decimal"]["exponent"] = 0
    if field == "locator": payload["rows"][0]["identifier_record"]["locator"]["logical_record"] = 2
    if field == "counts": payload["counts"]["cell_count"] = 0
    if field == "digest": payload["audit_id"] = "SYN_PRIVATE"
    if field == "binding": payload["input_binding"]["source_file_size"] += 1
    if field == "classification": payload["data_classification"] = "PUBLIC"
    with pytest.raises(MeasurementAuditError, match="INVALID_MEASUREMENT_AUDIT_RESULT") as exc:
        load_measurement_audit_result(json.dumps(payload))
    assert "SYN_PRIVATE" not in str(exc.value)


def test_self_consistent_rehashed_cells_need_fresh_source_verification(tmp_path):
    column = contract()
    audit = audit_contract(column)
    path = table(tmp_path, ("7.00",))
    result = inspect_measurements(path, column, audit)
    payload = result.model_dump(mode="json")
    payload["rows"][0]["cells"][0].update(raw_value="9.25", parse_input="9.25", exact_decimal=dict(sign=0, digits=[9, 2, 5], exponent=-2))
    altered_rows = tuple(measurements.MeasurementAuditRow(**r) for r in payload["rows"])
    provisional = result.model_copy(update={"rows": altered_rows})
    payload["audit_id"] = "MEASUREMENTS_" + DatasetManifest.content_digest(provisional.identity_payload())
    loaded = load_measurement_audit_result(json.dumps(payload))
    assert loaded.rows[0].cells[0].raw_value == "9.25"
    with pytest.raises(MeasurementAuditError, match="MEASUREMENT_SOURCE_REPLAY_MISMATCH"):
        verify_measurement_source(path, loaded)
    assert verify_measurement_source(path, result).audit_id == result.audit_id
    table(tmp_path, ("8.00",))
    with pytest.raises(MeasurementAuditError, match="MEASUREMENT_SOURCE_REPLAY_MISMATCH"):
        verify_measurement_source(path, result)


@pytest.mark.parametrize("nested_at", ["result_model", "identifiers", "inspection", "manifest"])
def test_nested_legacy_model_copy_bypass_rejected_at_source_verification_boundary(tmp_path, nested_at):
    result = inspect(tmp_path, ("7",))
    manifest = result.identifiers.inspection.manifest.model_copy(update={"source_file_sha256": "0" * 64})
    inspection = result.identifiers.inspection.model_copy(update={"manifest": manifest})
    identifiers = result.identifiers.model_copy(update={"inspection": inspection})
    payload = result.model_dump(mode="python")
    if nested_at == "result_model": payload = result.model_copy(update={"identifiers": identifiers})
    if nested_at == "identifiers": payload["identifiers"] = identifiers
    if nested_at == "inspection": payload["identifiers"]["inspection"] = inspection
    if nested_at == "manifest": payload["identifiers"]["inspection"]["manifest"] = manifest
    with pytest.raises(MeasurementAuditError, match="INVALID_MEASUREMENT_AUDIT_RESULT"):
        verify_measurement_source(tmp_path / "synthetic.csv", payload)


def test_valid_mixed_inputs_and_deeply_copied_audit_contract_validation(tmp_path):
    column = contract()
    audit = audit_contract(column)
    path = table(tmp_path)
    mixed_column = column.model_dump(mode="python")
    mixed_column.update(primary_gene=column.primary_gene, measurements=column.measurements)
    mixed_audit = audit.model_dump(mode="python")
    mixed_audit["policies"] = audit.policies
    result = inspect_measurements(path, mixed_column, mixed_audit)
    mixed_result = result.model_dump(mode="python")
    mixed_result.update(identifiers=result.identifiers, rows=result.rows)
    assert verify_measurement_source(path, mixed_result).audit_id == result.audit_id
    invalid = audit.policies[0].model_copy(update={"strip_ascii_whitespace": "SYN_PRIVATE"})
    mixed_audit["policies"] = (invalid,)
    with pytest.raises(MeasurementAuditError, match="INVALID_MEASUREMENT_AUDIT_CONTRACT") as exc:
        inspect_measurements(path, column, mixed_audit)
    assert "SYN_PRIVATE" not in str(exc.value)


@pytest.mark.parametrize("suffix,delimiter,encoding", [(".csv", ",", "utf-8-sig"), (".tsv", "\t", "utf-16")])
def test_existing_formats_and_limits_still_apply(tmp_path, suffix, delimiter, encoding):
    column = contract()
    audit = audit_contract(column)
    path = table(tmp_path, ("001.20",), name="synthetic" + suffix, encoding=encoding, delimiter=delimiter)
    result = inspect_measurements(path, column, audit, encoding=encoding)
    assert result.rows[0].cells[0].raw_value == "001.20"
    assert verify_measurement_source(path, result).audit_id == result.audit_id
    with pytest.raises(tables.DatasetInspectionError, match="BYTE_LIMIT"):
        inspect_measurements(path, column, audit, encoding=encoding, max_bytes=1)


def cli_files(tmp_path):
    column = contract()
    audit = audit_contract(column, caller_context_text="SYN_PRIVATE_CONTEXT",
                           source_declarations=[dict(record_id="SYN_PRIVATE_RECORD", text="SYN_PRIVATE_SOURCE")])
    cpath, apath = tmp_path / "synthetic-column.json", tmp_path / "synthetic-audit.json"
    cpath.write_text(column.model_dump_json())
    apath.write_text(audit.model_dump_json())
    return cpath, apath, audit


@pytest.mark.parametrize("json_mode", [False, True])
def test_opt_in_cli_prints_only_counts_and_fixed_statuses(tmp_path, monkeypatch, capsys, json_mode):
    cpath, apath, audit = cli_files(tmp_path)
    path = table(tmp_path, ("SYN_PRIVATE_VALUE", "7"), identifiers=("SYN_PRIVATE_ID", "SYN_OTHER_ID"))
    monkeypatch.setattr(sys, "argv", ["SYN_PRIVATE_PROGRAM", str(path), "--column-contract", str(cpath),
                                    "--measurement-audit-contract", str(apath), *(["--json"] if json_mode else [])])
    assert main() == 0
    output = capsys.readouterr()
    assert not output.err
    for private in ("SYN_PRIVATE", str(tmp_path), audit.content_sha256, "synthetic_value", "synthetic_feature", "SYN_MEASURE"):
        assert private not in output.out
    if json_mode:
        summary = json.loads(output.out)
        assert summary["cell_count"] == 2 and summary["cell_status_counts"]["NON_NUMERIC"] == 1


@pytest.mark.parametrize("fault", ["no_column", "category", "annotation", "bad_audit", "missing_audit", "unknown_option"])
def test_cli_mode_conflicts_and_errors_never_leak_inputs(tmp_path, monkeypatch, capsys, fault):
    cpath, apath, _ = cli_files(tmp_path)
    argv = ["SYN_PRIVATE_PROGRAM", str(table(tmp_path)), "--measurement-audit-contract", str(apath)]
    if fault != "no_column": argv.extend(("--column-contract", str(cpath)))
    if fault == "category": argv.extend(("--category-condition", "SYN_PRIVATE_PATH"))
    if fault == "annotation": argv.extend(("--annotation-reference", "SYN_PRIVATE_PATH"))
    if fault == "bad_audit": apath.write_text('{"SYN_PRIVATE_FIELD":"SYN_PRIVATE_VALUE"}')
    if fault == "missing_audit": apath.unlink()
    if fault == "unknown_option": argv.append("--SYN_PRIVATE_OPTION")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert not output.out and "SYN_PRIVATE" not in output.err and str(tmp_path) not in output.err


def test_cli_declarations_and_source_each_read_once(tmp_path, monkeypatch, capsys):
    cpath, apath, _ = cli_files(tmp_path)
    path = table(tmp_path)
    opened = []
    original = Path.open
    def observe(self, *args, **kwargs):
        if self in (cpath, apath, path): opened.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observe)
    monkeypatch.setattr(sys, "argv", ["audit", str(path), "--column-contract", str(cpath), "--measurement-audit-contract", str(apath)])
    assert main() == 0 and opened == [cpath, apath, path]
    assert "SYN_PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("invalid_locator", [True, 1.0])
def test_public_result_model_rejects_rehashed_noninteger_locator_in_nested_row_instance(tmp_path, invalid_locator):
    result = inspect(tmp_path, ("7",))
    record = result.rows[0].identifier_record
    boolean_locator = record.locator.model_copy(update={"logical_record": invalid_locator})
    bad_row = result.rows[0].model_copy(update={"identifier_record": record.model_copy(update={"locator": boolean_locator})})
    identity = result.identity_payload()
    identity["rows"][0]["identifier_record"]["locator"]["logical_record"] = invalid_locator
    payload = result.model_dump(mode="python")
    payload.update(rows=(bad_row,), audit_id="MEASUREMENTS_" + DatasetManifest.content_digest(identity))
    with pytest.raises(ValidationError):
        MeasurementAuditResult.model_validate(payload)
    valid_mixed = result.model_dump(mode="python")
    valid_mixed.update(rows=result.rows, identifiers=result.identifiers)
    assert MeasurementAuditResult.model_validate(valid_mixed) == result


def test_cell_limit_checked_before_cell_construction_and_applies_to_fresh_replay(tmp_path, monkeypatch):
    columns = ("synthetic_value", "synthetic_other")
    column = contract(columns=columns)
    audit = audit_contract(column)
    path = table(tmp_path, (("", ""), ("", ""), ("", "")), columns=columns)
    result = inspect_measurements(path, column, audit, max_cells=6)
    assert result.counts.cell_count == 6
    with pytest.raises(MeasurementAuditError, match="MEASUREMENT_CELL_LIMIT"):
        verify_measurement_source(path, result, max_cells=5)
    def forbidden(*args, **kwargs): raise AssertionError("cell constructed before memory limit")
    monkeypatch.setattr(measurements, "_observe", forbidden)
    with pytest.raises(MeasurementAuditError, match="MEASUREMENT_CELL_LIMIT"):
        inspect_measurements(path, column, audit, max_cells=5)


@pytest.mark.parametrize("limit", [0, -1, True, "1", 1.0])
def test_cell_limit_requires_positive_strict_integer(tmp_path, limit):
    column = contract()
    with pytest.raises(MeasurementAuditError, match="INVALID_MEASUREMENT_CELL_LIMIT"):
        inspect_measurements(table(tmp_path), column, audit_contract(column), max_cells=limit)


@pytest.mark.parametrize("role", ["P_VALUE", "ADJUSTED_P_VALUE", "EFFECT"])
def test_declared_scientific_role_never_adds_numeric_domain_or_direction_rules(tmp_path, role):
    column = contract(measurements=[dict(column="synthetic_value", role=role, definition="Invented caller role only")])
    result = inspect_measurements(table(tmp_path, ("-7.25", "9000")), column, audit_contract(column))
    assert all(r.cells[0].status == "FINITE_DECIMAL" for r in result.rows)
    assert result.identifiers.column_contract.measurements[0].role == role
    assert result.scientific_validation == "NOT_PERFORMED"
