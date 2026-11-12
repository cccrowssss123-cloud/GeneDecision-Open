"""6B2 uses invented tables/references only; no biological facts or databases."""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from genedecision.discovery import (
    ColumnContract, DatasetInspectionError, IdentifierInspection,
    IdentifierProcessingError, LexicalPolicy, LocalMappingReference, MappingEntry,
    inspect_identifiers, inspect_table, load_column_contract, load_mapping_reference,
)
from genedecision.discovery import tables
from genedecision.discovery.__main__ import main
from test_discovery_tables import no_runtime_dependencies  # shared synthetic dependency guard

pytestmark = pytest.mark.usefixtures("no_runtime_dependencies")
STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def contract_payload():
    return dict(primary_gene=dict(column="feature", definition="Synthetic source identifier"),
                measurements=[dict(column="score", definition="Synthetic arbitrary measurement")],
                namespace="synthetic-source", taxon="synthetic-taxon")


def contract(**updates):
    return ColumnContract.model_validate(dict(contract_payload(), **updates))


def write_table(tmp_path, text="feature,score\nSYNTHETIC_001,0.25\n", suffix=".csv", encoding="utf-8"):
    path = tmp_path / ("synthetic" + suffix)
    path.write_bytes(text.encode(encoding))
    return path


def reference(entries):
    return LocalMappingReference.from_entries(
        reference_id="synthetic-reference-v1", source_namespace="synthetic-source",
        target_namespace="synthetic-target", taxon="synthetic-taxon",
        entries=tuple(MappingEntry(source=s, target=t) for s, t in entries),
    )


def test_contract_and_local_result_roundtrip_are_versioned_and_bound(tmp_path):
    c = contract()
    assert load_column_contract(c.model_dump_json()) == c
    assert ColumnContract.model_json_schema()["properties"]["schema_version"]["const"] == "discovery-column-contract-v1"
    result = inspect_identifiers(write_table(tmp_path), c, inspected_at=STAMP)
    assert IdentifierInspection.model_validate_json(result.model_dump_json()) == result
    assert result.inspection.manifest.schema_version == "dataset-manifest-v1"
    assert result.contract_sha256 == c.content_sha256
    assert result.data_classification == "LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"
    assert result.inspection.manifest.explicit_mappings[1].definition == c.measurements[0].definition
    assert result.inspection.manifest.columns[1].tentative_role.value == "UNKNOWN"
    assert result.safe_summary()["scientific_validation"] == "NOT_PERFORMED"
    assert result.identifiers[0].status == "UNRESOLVED_NO_REFERENCE"


@pytest.mark.parametrize("updates", [
    {"schema_version": "v999"}, {"unknown": "SYNTHETIC_PRIVATE"},
    {"namespace": " "}, {"namespace": 1}, {"taxon": ""}, {"taxon": True},
    {"primary_gene": {"column": "feature", "definition": " "}},
    {"primary_gene": {"column": "feature", "role": "MEASUREMENT", "definition": "synthetic"}},
    {"measurements": []},
    {"measurements": [{"column": "score"}]},
    {"measurements": [{"column": "score", "definition": " "}]},
    {"measurements": [{"column": "score", "role": "GENE_IDENTIFIER", "definition": "synthetic"}]},
    {"measurements": [{"column": "feature", "definition": "synthetic"}]},
    {"measurements": [{"column": "score", "definition": "synthetic"}] * 2},
    {"lexical_policy": {"strip_whitespace": "true"}},
    {"lexical_policy": {"case": "AUTO"}},
    {"missing_identifier_tokens": [1]},
    {"missing_identifier_tokens": ["NA", "NA"]},
])
def test_invalid_contracts_fail_safely_at_public_boundary(tmp_path, updates):
    payload = dict(contract_payload(), **updates)
    raw = json.dumps(payload)
    with pytest.raises(IdentifierProcessingError) as exc:
        load_column_contract(raw)
    assert str(exc.value) == "INVALID_COLUMN_CONTRACT: local declaration or identifier processing failed"
    assert "SYNTHETIC_PRIVATE" not in str(exc.value)
    with pytest.raises(IdentifierProcessingError, match="INVALID_COLUMN_CONTRACT"):
        inspect_identifiers(write_table(tmp_path), payload)


@pytest.mark.parametrize("gene,measurement", [("Feature", "score"), ("feature", " score"), ("absent", "score")])
def test_selection_requires_exact_actual_headers(tmp_path, gene, measurement):
    c = contract(primary_gene=dict(column=gene, definition="synthetic"),
                 measurements=[dict(column=measurement, definition="synthetic")])
    with pytest.raises(DatasetInspectionError, match="INVALID_MAPPING"):
        inspect_identifiers(write_table(tmp_path), c)


@pytest.mark.parametrize("text,code", [
    ("feature,feature,score\nSYNTHETIC_A,SYNTHETIC_B,1\n", "DUPLICATE_HEADER"),
    ("feature,score\nSYNTHETIC_A\n", "MALFORMED_ROW_WIDTH"),
    ('feature,score\n"SYNTHETIC_A,1\n', "MALFORMED_QUOTING"),
])
def test_strict_structure_remains_in_force(tmp_path, text, code):
    with pytest.raises(DatasetInspectionError, match=code):
        inspect_identifiers(write_table(tmp_path, text), contract())


def test_raw_strings_missingness_duplicates_and_multiline_locators(tmp_path):
    text = ('feature,score\n00123,0\nENSSYNTHETIC0001.2,1\nSynMixed,2\n'
            'synmixed,3\n1e10,4\n2026-01-01,5\n" SYNTHETIC_A ",6\n'
            'NA,7\n,8\n"  ",9\n00123,10\n"SYNTHETIC_multi\nline",11\n\n')
    result = inspect_identifiers(write_table(tmp_path, text), contract())
    raw = [r.raw_identifier for r in result.identifiers]
    assert raw == ["00123", "ENSSYNTHETIC0001.2", "SynMixed", "synmixed", "1e10", "2026-01-01",
                   " SYNTHETIC_A ", "NA", "", "  ", "00123", "SYNTHETIC_multi\nline"]
    assert [r.lexical_identifier for r in result.identifiers] == [s if s.strip() else None for s in raw]
    assert result.identifiers[7].status == "UNRESOLVED_NO_REFERENCE"
    assert result.identifiers[8].status == result.identifiers[9].status == "BLANK"
    assert result.identifiers[0].raw_occurrence_count == result.identifiers[10].lexical_occurrence_count == 2
    assert result.identifiers[-1].locator.logical_record == 12
    assert result.identifiers[-1].locator.physical_line_end == 14
    assert result.inspection.manifest.blank_record_count == 1
    assert result.inspection.manifest.columns[0].numeric_like_value_count is None


def test_explicit_lexical_policy_records_collisions_and_exact_missing_tokens(tmp_path):
    c = contract(lexical_policy=dict(strip_whitespace=True, case="UPPER"),
                 missing_identifier_tokens=["NA"])
    result = inspect_identifiers(write_table(tmp_path, "feature,score\n Syn_A ,1\nSYN_A,2\nNA,3\nna,4\n"), c)
    rows = result.identifiers
    assert [r.raw_identifier for r in rows] == [" Syn_A ", "SYN_A", "NA", "na"]
    assert [r.lexical_identifier for r in rows] == ["SYN_A", "SYN_A", None, "NA"]
    assert [r.lexical_occurrence_count for r in rows] == [2, 2, 0, 1]
    assert rows[2].status == "DECLARED_MISSING"
    assert result.column_contract.lexical_policy == LexicalPolicy(strip_whitespace=True, case="UPPER")
    assert result.safe_summary()["raw_duplicate_row_count"] == 0
    assert result.safe_summary()["lexical_duplicate_row_count"] == 2
    lower = inspect_identifiers(write_table(tmp_path, "feature,score\nSyn_A.2,1\n"),
                                contract(lexical_policy={"case": "LOWER"}))
    assert lower.identifiers[0].lexical_identifier == "syn_a.2"


def test_reference_resolution_is_declared_local_exact_and_ambiguity_preserving(tmp_path):
    ref = reference([("SYN_A", "TARGET_A"), ("SYN_A", "TARGET_A"),
                     ("SYN_B", "TARGET_B1"), ("SYN_B", "TARGET_B2")])
    assert load_mapping_reference(ref.model_dump_json()) == ref
    c = contract(mapping_reference=ref.binding)
    result = inspect_identifiers(write_table(tmp_path, "feature,score\nSYN_A,1\nSYN_B,2\nSYN_C,3\n,4\n"),
                                 c, mapping_reference=ref)
    assert [r.status for r in result.identifiers] == ["RESOLVED_BY_LOCAL_REFERENCE", "UNRESOLVED_AMBIGUOUS", "UNRESOLVED_NOT_FOUND", "BLANK"]
    assert result.identifiers[0].mapping_entry_count == 2
    assert result.identifiers[0].mapping_targets == ("TARGET_A",)
    assert result.identifiers[1].mapping_targets == ("TARGET_B1", "TARGET_B2")
    assert result.inspection.manifest.row_count == 4
    assert result.column_contract.mapping_reference.content_sha256 == ref.content_sha256
    assert IdentifierInspection.model_validate_json(result.model_dump_json()) == result


def test_no_reference_is_read_implicitly_and_reference_keys_are_not_normalized(tmp_path):
    ref = reference([(" Syn_A ", "TARGET_A")])
    c = contract(mapping_reference=ref.binding, lexical_policy={"strip_whitespace": True, "case": "UPPER"})
    path = write_table(tmp_path, "feature,score\n Syn_A ,1\n")
    assert inspect_identifiers(path, c).identifiers[0].status == "UNRESOLVED_REFERENCE_NOT_SUPPLIED"
    assert inspect_identifiers(path, c, mapping_reference=ref).identifiers[0].status == "UNRESOLVED_NOT_FOUND"
    with pytest.raises(IdentifierProcessingError, match="MAPPING_REFERENCE_BINDING_MISMATCH"):
        inspect_identifiers(path, contract(), mapping_reference=ref)
    other = reference([(" Syn_A ", "TARGET_OTHER")])
    with pytest.raises(IdentifierProcessingError, match="MAPPING_REFERENCE_BINDING_MISMATCH"):
        inspect_identifiers(path, c, mapping_reference=other)


@pytest.mark.parametrize("field,value", [("source_namespace", "other-synthetic"), ("taxon", "other-synthetic-taxon")])
def test_reference_namespace_and_taxon_must_match_declaration(field, value):
    payload = reference([]).binding.model_dump()
    payload[field] = value
    with pytest.raises(ValidationError, match="CONTRACT_REFERENCE_DECLARATION_CONFLICT"):
        contract(mapping_reference=payload)


def test_reference_digest_is_checked_and_sensitive_errors_are_sanitized():
    payload = reference([("SYN_A", "TARGET_A")]).model_dump(mode="json")
    payload["entries"][0]["target"] = "SYNTHETIC_PRIVATE_TARGET"
    with pytest.raises(IdentifierProcessingError, match="INVALID_MAPPING_REFERENCE") as exc:
        load_mapping_reference(json.dumps(payload))
    assert "SYNTHETIC_PRIVATE_TARGET" not in str(exc.value)


def test_source_replacement_after_snapshot_cannot_change_identifiers_or_hash(tmp_path, monkeypatch):
    original = b"feature,score\nSYN_ORIGINAL,0.25\n"
    path = write_table(tmp_path, original.decode())
    reader = tables.read_evidence_rows
    snapshots = []
    def mutate_source(snapshot, **kwargs):
        snapshots.append(snapshot)
        assert Path(snapshot) != path
        path.write_text("feature,score\nSYN_REPLACED,100\nSYN_NEW,200\n")
        return reader(snapshot, **kwargs)
    monkeypatch.setattr(tables, "read_evidence_rows", mutate_source)
    result = inspect_identifiers(path, contract())
    assert result.identifiers[0].raw_identifier == "SYN_ORIGINAL"
    assert result.inspection.manifest.source_file_sha256 == hashlib.sha256(original).hexdigest()
    assert len(result.identifiers) == result.inspection.manifest.row_count == 1
    assert len(snapshots) == 1 and not snapshots[0].exists()


def test_contract_changes_identity_but_timestamp_does_not(tmp_path):
    path = write_table(tmp_path)
    first = inspect_identifiers(path, contract(), inspected_at=STAMP)
    changed = inspect_identifiers(path, contract(namespace="other-synthetic-source"), inspected_at=STAMP)
    later = inspect_identifiers(path, contract(), inspected_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert first.inspection.manifest.source_file_sha256 == changed.inspection.manifest.source_file_sha256
    assert first.processing_id != changed.processing_id
    assert first.processing_id == later.processing_id
    baseline = inspect_table(path, inspected_at=STAMP)
    assert baseline.manifest.explicit_mappings == ()
    assert baseline.manifest.columns[0].tentative_role.value == "UNKNOWN"


@pytest.mark.parametrize("field,value", [
    ("contract_sha256", "0" * 64), ("processing_id", "unbound"),
    ("reference_supplied", True), ("data_classification", "PUBLIC"),
])
def test_serialized_result_rejects_unbound_or_misclassified_content(tmp_path, field, value):
    payload = inspect_identifiers(write_table(tmp_path), contract()).model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        IdentifierInspection.model_validate(payload)


def test_copied_unvalidated_contract_is_revalidated_at_api_boundary(tmp_path):
    corrupted = contract().model_copy(update={"namespace": " "})
    with pytest.raises(IdentifierProcessingError, match="INVALID_COLUMN_CONTRACT"):
        inspect_identifiers(write_table(tmp_path), corrupted)


@pytest.mark.parametrize("suffix,delimiter,encoding", [(".csv", ",", "utf-8-sig"), (".tsv", "\t", "utf-16")])
def test_identifier_mode_reuses_supported_encodings_and_formats(tmp_path, suffix, delimiter, encoding):
    path = write_table(tmp_path, f"feature{delimiter}score\n00123{delimiter}0.25\n", suffix, encoding)
    result = inspect_identifiers(path, contract(), encoding=encoding)
    assert result.identifiers[0].raw_identifier == "00123"
    assert result.inspection.manifest.input_format == ("CSV" if suffix == ".csv" else "TSV")


@pytest.mark.parametrize("json_mode", [False, True])
def test_contract_cli_prints_only_counts_status_and_never_declared_or_raw_data(tmp_path, capsys, monkeypatch, json_mode):
    c = contract(primary_gene=dict(column="SYNTHETIC_PRIVATE_HEADER", definition="SYNTHETIC_PRIVATE_DEFINITION"))
    path = write_table(tmp_path, "SYNTHETIC_PRIVATE_HEADER,score\nSYNTHETIC_PRIVATE_CELL,0.25\n")
    contract_path = tmp_path / "synthetic_private_contract.json"
    contract_path.write_text(c.model_dump_json())
    argv = ["SYNTHETIC_PRIVATE_PROGRAM", str(path), "--column-contract", str(contract_path)]
    monkeypatch.setattr(sys, "argv", argv + (["--json"] if json_mode else []))
    assert main() == 0
    output = capsys.readouterr()
    assert not output.err
    for secret in (str(path), str(contract_path), "SYNTHETIC_PRIVATE", "feature", "score", c.content_sha256):
        assert secret not in output.out
    if json_mode:
        assert json.loads(output.out)["status_counts"] == {"UNRESOLVED_NO_REFERENCE": 1}
    else:
        assert "1 data rows, 2 columns" in output.out


def test_cli_local_reference_is_explicit_and_output_does_not_export_targets(tmp_path, capsys, monkeypatch):
    ref = reference([("SYNTHETIC_001", "SYNTHETIC_PRIVATE_TARGET")])
    c = contract(mapping_reference=ref.binding)
    contract_path = tmp_path / "synthetic_contract.json"
    reference_path = tmp_path / "synthetic_reference.json"
    contract_path.write_text(c.model_dump_json())
    reference_path.write_text(ref.model_dump_json())
    monkeypatch.setattr(sys, "argv", ["inspect", str(write_table(tmp_path)), "--column-contract", str(contract_path), "--mapping-reference", str(reference_path), "--json"])
    assert main() == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status_counts"] == {"RESOLVED_BY_LOCAL_REFERENCE": 1}
    assert "SYNTHETIC_PRIVATE" not in output and str(tmp_path) not in output


@pytest.mark.parametrize("args", [
    ["--encoding", "SYNTHETIC_PRIVATE_ENCODING"],
    ["--format", "SYNTHETIC_PRIVATE_FORMAT"],
    ["--SYNTHETIC_PRIVATE_OPTION"], ["--column-contract"],
    ["--mapping-reference", "SYNTHETIC_PRIVATE_PATH"],
])
def test_argument_errors_do_not_echo_sensitive_values(tmp_path, capsys, monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["SYNTHETIC_PRIVATE_PROGRAM", str(tmp_path / "SYNTHETIC_PRIVATE.csv"), *args])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err == "CLI_ARGUMENT_ERROR\n"


@pytest.mark.parametrize("payload", ['{"SYNTHETIC_PRIVATE_FIELD":"SYNTHETIC_PRIVATE_VALUE"}', '{"invalid":', '["SYNTHETIC_PRIVATE"]'])
def test_cli_contract_errors_hide_paths_fields_and_pydantic_inputs(tmp_path, capsys, monkeypatch, payload):
    cpath = tmp_path / "SYNTHETIC_PRIVATE.json"
    cpath.write_text(payload)
    monkeypatch.setattr(sys, "argv", ["inspect", str(write_table(tmp_path)), "--column-contract", str(cpath)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert not output.out and "INVALID_COLUMN_CONTRACT" in output.err
    assert "SYNTHETIC_PRIVATE" not in output.err and str(tmp_path) not in output.err


def test_multiple_measurement_definitions_are_retained_without_scientific_validation(tmp_path):
    c = contract(measurements=[dict(column="score", role="MEASUREMENT", definition="Synthetic arbitrary score"),
                               dict(column="other", role="P_VALUE", definition="Synthetic caller declaration only")])
    path = write_table(tmp_path, "feature,score,other\nSYN_A,1,2\n")
    result = inspect_identifiers(path, c)
    assert len(result.column_contract.measurements) == 2
    assert result.identifiers[0].status == "UNRESOLVED_NO_REFERENCE"
    assert result.safe_summary()["scientific_validation"] == "NOT_PERFORMED"
    assert all(m.definition for m in result.inspection.manifest.explicit_mappings)
    assert result.inspection.manifest.mapping_semantics == "USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"


def test_legacy_default_cli_omits_headers_hashes_and_raw_values(tmp_path, capsys, monkeypatch):
    path = write_table(tmp_path, "SYNTHETIC_PRIVATE_HEADER,score\nSYNTHETIC_PRIVATE_CELL,0.25\n")
    monkeypatch.setattr(sys, "argv", ["inspect", str(path)])
    assert main() == 0
    output = capsys.readouterr().out
    assert "1 data rows, 2 columns" in output
    assert "SYNTHETIC_PRIVATE" not in output and str(path) not in output
    assert "score" not in output and hashlib.sha256(path.read_bytes()).hexdigest() not in output


@pytest.mark.parametrize("unavailable", ["contract", "reference", "table"])
def test_cli_unavailable_files_hide_paths(tmp_path, capsys, monkeypatch, unavailable):
    path = write_table(tmp_path)
    cpath = tmp_path / "synthetic_contract.json"
    cpath.write_text(contract().model_dump_json())
    missing = tmp_path / "SYNTHETIC_PRIVATE_MISSING.json"
    argv = ["inspect", str(missing if unavailable == "table" else path), "--column-contract",
            str(missing if unavailable == "contract" else cpath)]
    if unavailable == "reference":
        argv += ["--mapping-reference", str(missing)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert not output.out and str(tmp_path) not in output.err and "SYNTHETIC_PRIVATE" not in output.err


def test_cli_invalid_reference_hides_field_names_and_values(tmp_path, capsys, monkeypatch):
    cpath = tmp_path / "synthetic_contract.json"
    rpath = tmp_path / "synthetic_reference.json"
    cpath.write_text(contract().model_dump_json())
    rpath.write_text('{"SYNTHETIC_PRIVATE_FIELD":"SYNTHETIC_PRIVATE_VALUE"}')
    monkeypatch.setattr(sys, "argv", ["inspect", str(write_table(tmp_path)), "--column-contract", str(cpath), "--mapping-reference", str(rpath)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "INVALID_MAPPING_REFERENCE" in output.err and "SYNTHETIC_PRIVATE" not in output.err


@pytest.mark.parametrize("location", ["top", "gene_column", "measurement_definition"])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_contract_loader_recursively_rejects_duplicate_json_keys(location, as_bytes):
    text = contract().model_dump_json()
    if location == "top":
        text = '{"namespace":"SYNTHETIC_PRIVATE_OVERWRITE",' + text[1:]
    elif location == "gene_column":
        text = text.replace('"column":"feature"', '"column":"SYNTHETIC_PRIVATE_OVERWRITE","column":"feature"')
    else:
        text = text.replace('"definition":"Synthetic arbitrary measurement"',
                            '"definition":"SYNTHETIC_PRIVATE_OVERWRITE","definition":"Synthetic arbitrary measurement"')
    with pytest.raises(IdentifierProcessingError) as exc:
        load_column_contract(text.encode("utf-8") if as_bytes else text)
    assert str(exc.value) == "INVALID_COLUMN_CONTRACT: local declaration or identifier processing failed"


@pytest.mark.parametrize("location", ["top", "entry_source", "entry_target"])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_reference_loader_recursively_rejects_duplicate_json_keys(location, as_bytes):
    text = reference([("SYN_A", "TARGET_A")]).model_dump_json()
    if location == "top":
        text = '{"reference_id":"SYNTHETIC_PRIVATE_OVERWRITE",' + text[1:]
    elif location == "entry_source":
        text = text.replace('"source":"SYN_A"', '"source":"SYNTHETIC_PRIVATE_OVERWRITE","source":"SYN_A"')
    else:
        text = text.replace('"target":"TARGET_A"', '"target":"SYNTHETIC_PRIVATE_OVERWRITE","target":"TARGET_A"')
    # Last-value parsing would otherwise yield an unchanged, valid digest.
    with pytest.raises(IdentifierProcessingError) as exc:
        load_mapping_reference(text.encode("utf-8") if as_bytes else text)
    assert str(exc.value) == "INVALID_MAPPING_REFERENCE: local declaration or identifier processing failed"


@pytest.mark.parametrize("loader,code", [(load_column_contract, "INVALID_COLUMN_CONTRACT"),
                                         (load_mapping_reference, "INVALID_MAPPING_REFERENCE")])
def test_public_json_loaders_reject_nonstandard_constants(loader, code):
    with pytest.raises(IdentifierProcessingError, match=code):
        loader('{"SYNTHETIC_PRIVATE_FIELD": NaN}')


def test_cli_rejects_duplicate_contract_key_without_echoing_declarations(tmp_path, capsys, monkeypatch):
    cpath = tmp_path / "synthetic_contract.json"
    cpath.write_text('{"namespace":"SYNTHETIC_PRIVATE_OVERWRITE",' + contract().model_dump_json()[1:])
    monkeypatch.setattr(sys, "argv", ["inspect", str(write_table(tmp_path)), "--column-contract", str(cpath)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "INVALID_COLUMN_CONTRACT" in output.err and "SYNTHETIC_PRIVATE" not in output.err
    assert not output.out
