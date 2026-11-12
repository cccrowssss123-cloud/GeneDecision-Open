"""Synthetic-only structural inspection, including side-effect/privacy guards."""

import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from genedecision.discovery import (
    ColumnMapping, ColumnRole, DatasetInspectionError, DatasetManifest,
    DatasetType, inspect_table,
)
from genedecision.discovery import tables

STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_runtime_dependencies(monkeypatch):
    """No transport, storage or scientific provider may be used by inspection."""
    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery inspection invoked an external/scientific dependency")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    from genedecision.storage import EvidenceDatabase
    from genedecision.interpretation.core import InterpretationService
    from genedecision.decision.core import DecisionService
    monkeypatch.setattr(EvidenceDatabase, "__init__", forbidden)
    monkeypatch.setattr(InterpretationService, "interpret", forbidden)
    monkeypatch.setattr(DecisionService, "decide", forbidden)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture
def table(tmp_path):
    def write(text, suffix=".csv", encoding="utf-8"):
        path = tmp_path / ("synthetic" + suffix)
        path.write_bytes(text.encode(encoding))
        return path
    return write


def codes(items):
    return {item.code for item in items}


@pytest.mark.parametrize("suffix,delimiter,format_name", [(".csv", ",", "CSV"), (".tsv", "\t", "TSV")])
def test_valid_table_needs_no_gene_hypothesis_or_platform(table, suffix, delimiter, format_name):
    path = table(delimiter.join(["gene_id", "effect", "p_value"]) + "\n"
                 + delimiter.join(["000123", "-2.5", "0.01"]) + "\n", suffix)
    result = inspect_table(path, inspected_at=STAMP)
    m = result.manifest
    assert (m.row_count, m.column_count, m.input_format, m.delimiter) == (1, 3, format_name, delimiter)
    assert m.ordered_column_names == ("gene_id", "effect", "p_value")
    assert m.dataset_type is DatasetType.UNKNOWN
    assert m.dataset_type_basis == "NOT_ESTABLISHED"
    assert m.explicit_mappings == ()
    assert m.columns[0].tentative_role is ColumnRole.GENE_IDENTIFIER
    assert "SELECT_GENE_COLUMN" in codes(result.unresolved_mapping_questions)
    assert "MEASUREMENT_SEMANTICS_UNRESOLVED" in codes(result.unresolved_mapping_questions)
    assert "Structural inspection only" in result.summary


@pytest.mark.parametrize("suffix,content", [
    (".csv", 'gene,comment\n"G,1","some, quoted text"\n'),
    (".tsv", 'gene\tcomment\n"G\t1"\t"some\tquoted text"\n'),
])
def test_quoted_delimiters_are_preserved_by_existing_reader(table, monkeypatch, suffix, content):
    seen = []
    original = tables.read_evidence_rows
    def capture(*args, **kwargs):
        rows = original(*args, **kwargs)
        seen.extend(rows)
        return rows
    monkeypatch.setattr(tables, "read_evidence_rows", capture)
    result = inspect_table(table(content, suffix), inspected_at=STAMP)
    assert result.manifest.row_count == 1
    assert seen[0].values["gene"] == ("G,1" if suffix == ".csv" else "G\t1")
    assert seen[0].values["comment"] == ("some, quoted text" if suffix == ".csv" else "some\tquoted text")


def test_escaped_quotes_multiline_fields_and_blank_records_have_locators(table, monkeypatch):
    content = 'gene,comment\nG1,"two\nlines and ""quotes"""\n\nG2,last\n'
    result = inspect_table(table(content), inspected_at=STAMP)
    assert [(r.logical_record, r.physical_line_end) for r in result.row_locators] == [(1, 3), (2, 5)]
    assert result.manifest.blank_record_count == 1
    assert "BLANK_RECORDS" in codes(result.manifest.warnings)
    assert "two" not in result.manifest.model_dump_json()


@pytest.mark.parametrize("content,code", [
    ("", "EMPTY_FILE"),
    ("gene,score\n", "EMPTY_TABLE"),
    ("gene,score\n\n", "EMPTY_TABLE"),
    ("\nG1,1\n", "MISSING_HEADER"),
    (",score\nG1,1\n", "MISSING_HEADER"),
    ("gene, \nG1,1\n", "MISSING_HEADER"),
    ("gene,gene\nG1,G2\n", "DUPLICATE_HEADER"),
    ("gene, gene \nG1,G2\n", "DUPLICATE_HEADER"),
    ("gene,score\nG1\n", "MALFORMED_ROW_WIDTH"),
    ("gene,score\nG1,1,extra\n", "MALFORMED_ROW_WIDTH"),
    ('gene,score\nG1,"unterminated\n', "MALFORMED_QUOTING"),
    ('gene,score\nG1,"1"junk\n', "MALFORMED_QUOTING"),
    ("0001,0.5\n0002,0.4\n", "POSSIBLE_MISSING_HEADER"),
])
def test_structural_errors_refuse_repairs_and_do_not_leak_paths(table, content, code):
    path = table(content)
    before = path.read_bytes()
    with pytest.raises(DatasetInspectionError) as error:
        inspect_table(path)
    assert error.value.code == code
    assert str(path) not in str(error.value)
    assert "synthetic.csv" not in str(error.value)
    assert path.read_bytes() == before


def test_row_width_error_identifies_physical_line_without_cells(table):
    with pytest.raises(DatasetInspectionError) as error:
        inspect_table(table("gene,p_value\nSECRET_IDENTIFIER,1,SECRET_VALUE\n"))
    assert error.value.row_number == 2
    assert "SECRET" not in str(error.value)


def test_intentional_numeric_headers_require_confirmation(table):
    path = table("gene,2026\nG1,1\n")
    with pytest.raises(DatasetInspectionError, match="POSSIBLE_MISSING_HEADER"):
        inspect_table(path)
    result = inspect_table(path, confirm_header=True, inspected_at=STAMP)
    assert result.manifest.ordered_column_names == ("gene", "2026")
    assert result.manifest.header_confirmed_by_caller is True


def test_identifier_strings_are_not_coerced_and_na_can_be_a_gene(table, monkeypatch):
    identifiers = ["000001", "1E10", "01/02", "12345678901234567890", " NA ", " ENSG000001.7 "]
    seen = []
    original = tables.read_evidence_rows
    def capture(*args, **kwargs):
        rows = original(*args, **kwargs)
        seen.extend(row.values["gene_id"] for row in rows)
        return rows
    monkeypatch.setattr(tables, "read_evidence_rows", capture)
    result = inspect_table(table("gene_id,score\n" + "".join(f"{gene},0\n" for gene in identifiers)))
    assert seen == identifiers
    profile = result.manifest.columns[0]
    assert profile.numeric_like_value_count is None
    assert profile.blank_value_count == profile.declared_missing_value_count == profile.possible_missing_marker_count == 0


def test_missing_identifiers_and_numbers_stay_missing_and_zero_stays_present(table):
    path = table("gene,p_value\n,\nG1,0\nG2,NA\nG3,0.5\n")
    m = inspect_table(path).manifest
    assert m.columns[0].blank_value_count == 1
    profile = m.columns[1]
    assert profile.blank_value_count == 1
    assert profile.declared_missing_value_count == 0
    assert profile.possible_missing_marker_count == 1
    assert profile.numeric_like_value_count == 2
    assert "CONFIRM_MISSING_TOKENS" in codes(m.unresolved_mapping_questions)
    declared = inspect_table(path, missing_tokens_by_column={"p_value": [" na ", "NA"]}).manifest
    assert declared.columns[1].declared_missing_value_count == 1
    assert declared.columns[1].possible_missing_marker_count == 0
    assert declared.columns[1].numeric_like_value_count == 2
    assert declared.columns[1].missing_tokens == ("na",)


def test_ambiguous_gene_headers_remain_suggestions(table):
    m = inspect_table(table("symbol,gene_id,padj\nG1,0001,0.1\n")).manifest
    assert m.explicit_mappings == ()
    assert "AMBIGUOUS_GENE_COLUMNS" in codes(m.warnings)
    question = next(q for q in m.unresolved_mapping_questions if q.code == "SELECT_GENE_COLUMN")
    assert question.columns == ("symbol", "gene_id")


def test_unknown_score_column_is_never_a_defined_statistic(table):
    m = inspect_table(table("feature,score\nprivate_identifier,97\n")).manifest
    assert m.columns[1].tentative_role is ColumnRole.UNKNOWN
    assert "NO_GENE_COLUMN_HINT" in codes(m.warnings)
    assert any(q.code == "COLUMN_ROLE_UNRESOLVED" and q.columns == ("score",) for q in m.unresolved_mapping_questions)
    assert "private_identifier" not in m.model_dump_json()
    assert "SIGNAL_PRESENT" not in m.model_dump_json()
    assert "MEASURED_NO_SIGNIFICANT_SIGNAL" not in m.model_dump_json()


def test_non_numeric_statistic_is_flagged_without_scientific_validation(table):
    m = inspect_table(table("gene,p_value\nG1,<0.001\nG2,infinity\nG3,2.0\n")).manifest
    assert m.columns[1].numeric_like_value_count == 1
    assert "NON_NUMERIC_MEASUREMENT_TOKENS" in codes(m.warnings)
    # Structural inspection does not clip 2.0 or certify it as a p-value.
    assert "MEASUREMENT_SEMANTICS_UNRESOLVED" in codes(m.unresolved_mapping_questions)


def test_explicit_mappings_are_structurally_checked_but_not_scientifically_validated(table):
    path = table("feature,metric\n0001,0.4\n")
    result = inspect_table(path, mappings=[
        ColumnMapping(column="metric", role=ColumnRole.P_VALUE, definition="user supplied definition"),
        ColumnMapping(column="feature", role=ColumnRole.GENE_IDENTIFIER),
    ], dataset_type=DatasetType.CRISPR, inspected_at=STAMP)
    m = result.manifest
    assert [item.column for item in m.explicit_mappings] == ["feature", "metric"]
    assert m.columns[0].numeric_like_value_count is None
    assert m.dataset_type_basis == "USER_DECLARED"
    assert "SELECT_GENE_COLUMN" not in codes(m.unresolved_mapping_questions)
    assert "GENE_NAMESPACE_UNRESOLVED" in codes(m.unresolved_mapping_questions)
    assert "MEASUREMENT_SEMANTICS_UNRESOLVED" in codes(m.unresolved_mapping_questions)


@pytest.mark.parametrize("mappings", [
    [ColumnMapping(column="absent", role=ColumnRole.GENE_IDENTIFIER)],
    [ColumnMapping(column="gene", role=ColumnRole.GENE_IDENTIFIER)] * 2,
])
def test_invalid_mapping_rejected(table, mappings):
    with pytest.raises(DatasetInspectionError, match="INVALID_MAPPING"):
        inspect_table(table("gene,score\nG1,1\n"), mappings=mappings)


@pytest.mark.parametrize("policy", [{"absent": ["NA"]}, {"score": "NA"}, {"score": [1]}])
def test_invalid_missing_policy_rejected(table, policy):
    with pytest.raises(DatasetInspectionError, match="INVALID_MISSING_POLICY"):
        inspect_table(table("gene,score\nG1,1\n"), missing_tokens_by_column=policy)


def test_hashes_are_path_and_time_independent_but_inspection_policy_bound(table, tmp_path):
    path = table("gene,score\nG1,NA\n")
    other = tmp_path / "different_private_name.csv"
    other.write_bytes(path.read_bytes())
    first = inspect_table(path, inspected_at=STAMP).manifest
    second = inspect_table(other, inspected_at=STAMP + timedelta(days=1)).manifest
    assert first.source_file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.source_file_size == path.stat().st_size
    assert first.dataset_id == second.dataset_id
    assert first.inspection_id == second.inspection_id
    assert first.identity_payload() == second.identity_payload()
    assert first.inspected_at != second.inspected_at
    adjusted = inspect_table(path, missing_tokens_by_column={"score": ["NA"]}).manifest
    assert adjusted.dataset_id == first.dataset_id
    assert adjusted.inspection_id != first.inspection_id
    path.write_text("gene,score\nG2,NA\n")
    assert inspect_table(path).manifest.dataset_id != first.dataset_id


def test_source_bytes_metadata_and_private_rows_not_exported(table):
    path = table("gene,comment\nPRIVATE_GENE,PRIVATE_NOTE\n")
    before_bytes = path.read_bytes()
    before = path.stat()
    result = inspect_table(path)
    after = path.stat()
    exported = result.model_dump_json()
    assert path.read_bytes() == before_bytes
    assert (before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    assert str(path.parent) not in exported
    assert path.name not in exported
    assert "PRIVATE_GENE" not in exported and "PRIVATE_NOTE" not in exported


def test_source_changed_during_raw_reader_uses_exact_hashed_snapshot(table, monkeypatch):
    path = table("gene,score\nG1,1\n")
    original_bytes = path.read_bytes()
    reader = tables.read_evidence_rows
    def replace_source(snapshot, **kwargs):
        path.write_text("gene,score\nG2,2\nG3,3\n")
        return reader(snapshot, **kwargs)
    monkeypatch.setattr(tables, "read_evidence_rows", replace_source)
    result = inspect_table(path)
    assert result.manifest.source_file_sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert result.manifest.row_count == 1


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_explicit_supported_encoding_with_bom(table, encoding):
    result = inspect_table(table("gene,comment\nG1,描述\n", encoding=encoding), encoding=encoding)
    assert result.manifest.ordered_column_names == ("gene", "comment")


def test_bad_encoding_rejected_without_guessing(table):
    path = table("gene,score\nG1,1\n")
    path.write_bytes(b"gene,score\nG1,\xff\n")
    with pytest.raises(DatasetInspectionError, match="INVALID_ENCODING"):
        inspect_table(path)


@pytest.mark.parametrize("content", ["gene,score\nG1,\x00\n", "\ufeffgene,score\nG1,1\n"])
def test_unhandled_nul_or_bom_rejected(table, content):
    with pytest.raises(DatasetInspectionError, match="INVALID_ENCODING"):
        inspect_table(table(content), encoding="utf-8")


def test_wrong_extension_is_not_silently_sniffed(table):
    path = table("gene\tscore\nG1\t1\n")
    wrong = inspect_table(path).manifest
    assert wrong.column_count == 1
    assert "SINGLE_COLUMN" in codes(wrong.warnings)
    confirmed = inspect_table(path, input_format="TSV").manifest
    assert confirmed.column_count == 2


def test_unsupported_file_types_and_missing_file_are_safe(table, tmp_path):
    with pytest.raises(DatasetInspectionError, match="UNSUPPORTED_FORMAT"):
        inspect_table(table("{}", ".json"))
    with pytest.raises(DatasetInspectionError, match="FILE_UNAVAILABLE") as error:
        inspect_table(tmp_path / "private_missing.csv")
    assert "private_missing" not in str(error.value)


def test_configured_limits_stop_large_inputs(table):
    path = table("gene,score\nG1,1\nG2,2\n")
    with pytest.raises(DatasetInspectionError, match="BYTE_LIMIT"):
        inspect_table(path, max_bytes=1)
    with pytest.raises(DatasetInspectionError, match="ROW_LIMIT"):
        inspect_table(path, max_rows=1)
    with pytest.raises(DatasetInspectionError, match="INVALID_LIMIT"):
        inspect_table(path, max_bytes=True)


def test_manifest_roundtrip_version_and_cross_field_tampering(table):
    manifest = inspect_table(table("gene,score\nG1,1\n"), inspected_at=STAMP).manifest
    assert DatasetManifest.model_validate_json(manifest.model_dump_json()) == manifest
    schema = DatasetManifest.model_json_schema()
    assert schema["properties"]["schema_version"]["const"] == "dataset-manifest-v1"
    for key, value in [("schema_version", "v999"), ("column_count", 20), ("dataset_id", "unbound"), ("dataset_type", "CRISPR"), ("source_file_sha256", "0" * 64), ("raw_rows", [])]:
        payload = manifest.model_dump(mode="json")
        payload[key] = value
        with pytest.raises(ValidationError):
            DatasetManifest.model_validate(payload)
    with pytest.raises(ValidationError, match="timezone"):
        inspect_table(table("gene,score\nG1,1\n"), inspected_at=datetime(2026, 1, 1))


def test_cli_prints_summary_and_json_without_database_or_credentials(table, capsys, monkeypatch):
    from genedecision.discovery.__main__ import main
    path = table("gene,score\nPRIVATE_GENE,PRIVATE_SCORE\n")
    monkeypatch.setattr(sys, "argv", ["inspect", str(path), "--json"])
    assert main() == 0
    exported = capsys.readouterr().out
    payload = json.loads(exported)
    assert payload["manifest"]["dataset_type"] == "UNKNOWN"
    assert payload["manifest"]["unresolved_mapping_questions"]
    assert "PRIVATE_GENE" not in exported and str(path) not in exported
    monkeypatch.setattr(sys, "argv", ["inspect", str(path)])
    assert main() == 0
    assert "1 data rows, 2 columns" in capsys.readouterr().out


def test_cli_error_hides_private_path(table, capsys, monkeypatch):
    from genedecision.discovery.__main__ import main
    path = table("gene,score\nPRIVATE_GENE\n")
    monkeypatch.setattr(sys, "argv", ["inspect", str(path)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "MALFORMED_ROW_WIDTH" in stderr
    assert str(path) not in stderr and "PRIVATE_GENE" not in stderr


def test_module_entrypoint_in_separate_process_is_local_only(table, tmp_path):
    path = table("gene,score\nG1,1\n")
    guard = tmp_path / "entrypoint_guard.py"
    guard.write_text(
        "import runpy, sys\n"
        "def guard(event, args):\n"
        "    if event.startswith('socket.') or event == 'sqlite3.connect':\n"
        "        raise RuntimeError('forbidden dependency')\n"
        "sys.addaudithook(guard)\n"
        "sys.argv = ['inspection', sys.argv[1], '--json']\n"
        "runpy.run_module('genedecision.discovery', run_name='__main__')\n"
    )
    import os
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.run([sys.executable, str(guard), str(path)], env=env, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["manifest"]["row_count"] == 1
