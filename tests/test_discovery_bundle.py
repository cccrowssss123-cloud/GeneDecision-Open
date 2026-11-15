"""Synthetic acceptance cases for the local CLI, bundle and multi-run union."""

import csv
import hashlib
import json
import os
import runpy
import stat
from pathlib import Path

import pytest

from genedecision.discovery import (
    BundleError, ColumnContract, DiscoveryRunError, LocalMappingReference,
    MappingEntry, MeasurementAuditContract, NumericCondition, NumericContext,
    inspect_measurements, run_discovery, union_discovery_runs,
    verify_discovery_bundle, write_discovery_bundle,
)
from genedecision.discovery import bundle
from genedecision.discovery.__main__ import main


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def prepare(tmp_path):
    def make(name, rows, *, taxon="synthetic-taxon", namespace="synthetic-key", threshold="5", fail_first=False):
        mapping = LocalMappingReference.from_entries(
            reference_id="SYN_MAPPING", source_namespace="synthetic-raw",
            target_namespace=namespace, taxon=taxon,
            entries=tuple(MappingEntry(source=raw, target=key) for raw, key in (
                ("SYN_A", "=SYN_KEY_A"), ("SYN_B", "SYN_KEY_B"), ("SYN_BOTH", "SYN_KEY_BOTH"),
            )),
        )
        columns = ColumnContract(
            primary_gene={"column": "feature", "definition": "Invented ID"},
            measurements=[{"column": "value", "definition": "Invented number"}],
            namespace=mapping.source_namespace, taxon=taxon, mapping_reference=mapping.binding,
        )
        audit_contract = MeasurementAuditContract.from_declarations(
            column_contract_sha256=columns.content_sha256,
            policies=[{"measurement_id": "SYN_M", "column": "value"}],
        )
        source = tmp_path / name
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t" if source.suffix == ".tsv" else ",")
            writer.writerow(("feature", "value", "synthetic_unused"))
            writer.writerows(rows)
        audit = inspect_measurements(source, columns, audit_contract, mapping_reference=mapping)
        context = None
        if fail_first:
            binding = {"source_id": "SYN_QC", "release": "SYN_RELEASE"}
            context = NumericContext.from_declarations(
                context_id="SYN_CONTEXT", release="SYN_RELEASE", measurement_audit_id=audit.audit_id,
                source_records=[{"record_id": "SYN_RECORD", **binding, "source_locator": "SYN_LOCATOR",
                                 "statement": "Invented declared failure"}],
                qc_declarations=[
                    {"declaration_id": "SYN_FAIL", "status": "DECLARED_FAIL", "source_binding": binding,
                     "source_record_ids": ["SYN_RECORD"], "measurement_ids": ["SYN_M"],
                     "scope": "ROWS", "locators": [audit.rows[0].identifier_record.locator]},
                ],
            )
        condition = NumericCondition.from_declarations(
            measurement_audit_id=audit.audit_id, context_sha256=context.content_sha256 if context else None,
            predicates=[{"predicate_id": "SYN_P", "measurement_id": "SYN_M", "operator": "GE",
                         "threshold": threshold, "provenance": "Invented comparison"}], combine="ALL",
        )
        options = {
            "mapping_reference": mapping, "measurement_audit_contract": audit_contract,
            "numeric_condition": condition, "numeric_context": context,
        }
        return source, columns, options
    return make


def test_bundle_preserves_all_rows_and_is_reproducible(prepare, tmp_path):
    source, columns, options = prepare(
        "synthetic.csv",
        [("SYN_A", "5", "001\nSYN_NOTE"), ("SYN_A", "1", "  "), ("SYN_A", "NA", "")],
        fail_first=True,
    )
    first = write_discovery_bundle(run_discovery(source, columns, **options), tmp_path / "one")
    second = write_discovery_bundle(run_discovery(source, columns, **options), tmp_path / "two")
    assert first == second == verify_discovery_bundle(tmp_path / "one")
    assert first.row_truth_counts == {"T": 1, "F": 1, "U": 1}
    assert first.numeric_qc_flagged_rows == first.candidate_count == 1
    destination = tmp_path / "one"
    observations = _lines(destination / "observations.jsonl")
    assert [record["raw_values"] for record in observations] == [
        ["SYN_A", "5", "001\nSYN_NOTE"], ["SYN_A", "1", "  "], ["SYN_A", "NA", ""],
    ]
    assert [record["locator"]["physical_line_end"] for record in observations] == [3, 4, 5]
    outcomes = _lines(destination / "row_outcomes.jsonl")
    assert [record["truth"] for record in outcomes] == ["T", "F", "U"]
    assert outcomes[0]["numeric"]["has_declared_qc_failure"]
    assert all(record["warning"] == "SCIENTIFICALLY_UNVERIFIED" for record in outcomes)
    entry, = _lines(destination / "candidates.jsonl")
    assert [locator["logical_record"] for locator in entry["observed_rows"]] == [1, 2, 3]
    assert entry["key"]["identifier"] == "=SYN_KEY_A"
    with (destination / "candidates.tsv").open(encoding="utf-8", newline="") as handle:
        row, = csv.DictReader(handle, delimiter="\t")
    assert row["identifier"] == "'=SYN_KEY_A"
    assert row["logical_record"] == "1" and row["declared_qc_failure"] == "true"
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in destination.iterdir())


def test_bundle_never_overwrites_and_integrity_checks_same_size_tampering(prepare, tmp_path):
    source, columns, options = prepare("table.csv", [("SYN_A", "5", "invented")])
    run = run_discovery(source, columns, **options)
    path = tmp_path / "export"
    manifest = write_discovery_bundle(run, path)
    with pytest.raises(BundleError, match="BUNDLE_DESTINATION_EXISTS"):
        write_discovery_bundle(run, path)
    assert verify_discovery_bundle(path) == manifest
    raw = path / "observations.jsonl"
    raw.write_bytes(raw.read_bytes().replace(b"SYN_A", b"SYN_B"))
    with pytest.raises(BundleError, match="BUNDLE_INVALID_OR_INCOMPLETE"):
        verify_discovery_bundle(path)


def test_equivalent_csv_and_tsv_keep_same_candidates_but_distinct_source_identity(prepare, tmp_path):
    rows = [("SYN_A", "5", "invented\nrecord"), ("SYN_B", "NA", "other")]
    csv_source, csv_columns, csv_options = prepare("table.csv", rows)
    tsv_source, tsv_columns, tsv_options = prepare("table.tsv", rows)
    csv_run = run_discovery(csv_source, csv_columns, **csv_options)
    tsv_run = run_discovery(tsv_source, tsv_columns, **tsv_options)
    assert csv_run.candidates == tsv_run.candidates
    assert csv_run.observations == tsv_run.observations
    csv_manifest = write_discovery_bundle(csv_run, tmp_path / "csv")
    tsv_manifest = write_discovery_bundle(tsv_run, tmp_path / "tsv")
    assert csv_manifest.source_sha256 != tsv_manifest.source_sha256
    assert csv_manifest.files["candidates.tsv"] == tsv_manifest.files["candidates.tsv"]


def test_failure_removes_partial_export_and_leaves_source_alone(prepare, tmp_path, monkeypatch):
    source, columns, options = prepare("table.csv", [("SYN_A", "5", "invented")])
    run = run_discovery(source, columns, **options)
    original = source.read_bytes()
    destination = tmp_path / "incomplete"

    def fail(source_file, dest_file):
        assert source_file.name == ".run_manifest.pending"
        assert not dest_file.exists()
        raise OSError("SYN_ERROR_TEXT")

    monkeypatch.setattr(bundle.os, "replace", fail)
    with pytest.raises(BundleError, match="BUNDLE_WRITE_FAILED") as error:
        write_discovery_bundle(run, destination)
    assert "SYN_ERROR_TEXT" not in str(error.value)
    assert not destination.exists() and source.read_bytes() == original


def test_cli_export_verify_rejects_stale_input_and_limits_stdout(prepare, tmp_path, monkeypatch, capsys):
    source, columns, options = prepare("SYN_PRIVATE_TABLE.tsv", [("SYN_A", "6", "SYN_PRIVATE_NOTE")])
    declared = {"column-contract": columns, **{key.replace("_", "-"): value for key, value in options.items()}}
    destination = tmp_path / "SYN_PRIVATE_FOLDER"
    args = ["discovery", str(source), "--export-bundle", str(destination), "--json"]
    for key, value in declared.items():
        if value is not None:
            path = tmp_path / f"{key}.json"
            path.write_text(value.model_dump_json(), encoding="utf-8")
            args.extend([f"--{key}", str(path)])
    args.extend(["--expected-source-sha256", hashlib.sha256(source.read_bytes()).hexdigest()])
    monkeypatch.setattr("sys.argv", args)
    assert main() == 0
    exported = capsys.readouterr()
    assert json.loads(exported.out)["status"] == "BUNDLE_WRITTEN"
    assert "SYN_PRIVATE" not in exported.out + exported.err
    source.write_text("feature\tvalue\tsynthetic_unused\nSYN_A\t8\tchanged\n", encoding="utf-8")
    args[3] = str(tmp_path / "stale")
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    stale = capsys.readouterr()
    assert "SHA256" in stale.err or "SOURCE" in stale.err
    assert "SYN_PRIVATE" not in stale.err and not (tmp_path / "stale").exists()
    monkeypatch.setattr("sys.argv", ["discovery", "--verify-bundle", str(destination), "--json"])
    assert main() == 0
    verified = capsys.readouterr()
    assert json.loads(verified.out)["status"] == "BUNDLE_INTEGRITY_VERIFIED"
    assert "SYN_PRIVATE" not in verified.out + verified.err


def test_union_includes_one_table_matches_retains_observed_failure_and_keeps_namespaces(prepare):
    def execute(name, rows, **options):
        path, columns, config = prepare(name, rows, **options)
        return run_discovery(path, columns, **config)

    a_rows = [("SYN_A", "8", "invented"), ("SYN_BOTH", "7", ""), ("SYN_BOTH", "NA", "")]
    a = execute("a.csv", a_rows)
    b = execute("b.tsv", [("SYN_B", "9", ""), ("SYN_BOTH", "1", "")])
    a_second = execute("duplicate.csv", a_rows, threshold="9")
    other_taxon = execute("different.csv", a_rows, taxon="synthetic-other")
    other_namespace = execute("namespace.csv", a_rows, namespace="synthetic-different")
    union = union_discovery_runs([b, a, a, a_second, other_taxon, other_namespace])
    assert union == union_discovery_runs([a, a_second, other_namespace, other_taxon, b])
    assert len(union.runs) == 5 and union.independence == "NOT_ESTABLISHED"
    assert len({ref.source_sha256 for ref in union.runs}) == 2
    candidates = {candidate.key.identifier: candidate for candidate in union.candidates
                  if candidate.key.taxon == "synthetic-taxon" and candidate.key.namespace == "synthetic-key"}
    assert set(candidates) == {"=SYN_KEY_A", "SYN_KEY_B", "SYN_KEY_BOTH"}
    assert len(union.candidates) == 7
    evidence = {ref.run_id: ref for ref in candidates["SYN_KEY_BOTH"].evidence}
    assert len(evidence[a.run_id].matching_rows) == len(evidence[a.run_id].unknown_rows) == 1
    assert len(evidence[b.run_id].nonmatching_rows) == 1
    assert all(ref.run_id != b.run_id for ref in candidates["=SYN_KEY_A"].evidence)
    assert len(union_discovery_runs([a]).candidates) == 2
    with pytest.raises(DiscoveryRunError, match="DISCOVERY_UNION_REQUIRES_RUN"):
        union_discovery_runs([])


def test_public_demo_runs_to_completion(capsys):
    demo = Path(__file__).resolve().parents[1] / "examples" / "discovery_bundle_demo.py"
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(demo), run_name="__main__")
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "SYNTHETIC_BUNDLE_DEMO_OK" in output and "SCIENTIFICALLY_UNVERIFIED" in output
