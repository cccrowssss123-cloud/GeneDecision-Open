"""Inspect one local analyzed table without inferring biological conclusions."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from genedecision.errors import EvidenceImportError, GeneDecisionError
from genedecision.ingestion.user_evidence import RawEvidenceRow, read_evidence_rows

from .models import (
    ColumnInspection, ColumnMapping, ColumnRole, DatasetManifest, DatasetType,
    InspectionWarning, MappingQuestion, RowLocator, TableInspection,
)

# Exact normalized header hints only. No aliases or biological memberships
# are resolved. "score" deliberately has no scientific role.
HEADER_HINTS = {
    "gene": ColumnRole.GENE_IDENTIFIER,
    "gene_symbol": ColumnRole.GENE_IDENTIFIER,
    "symbol": ColumnRole.GENE_IDENTIFIER,
    "gene_id": ColumnRole.GENE_IDENTIFIER,
    "ensembl_gene_id": ColumnRole.GENE_IDENTIFIER,
    "entrez_gene_id": ColumnRole.GENE_IDENTIFIER,
    "p_value": ColumnRole.P_VALUE,
    "pvalue": ColumnRole.P_VALUE,
    "pval": ColumnRole.P_VALUE,
    "p": ColumnRole.P_VALUE,
    "fdr": ColumnRole.ADJUSTED_P_VALUE,
    "padj": ColumnRole.ADJUSTED_P_VALUE,
    "q_value": ColumnRole.ADJUSTED_P_VALUE,
    "qvalue": ColumnRole.ADJUSTED_P_VALUE,
    "log2foldchange": ColumnRole.EFFECT,
    "log2_fold_change": ColumnRole.EFFECT,
    "logfc": ColumnRole.EFFECT,
    "effect": ColumnRole.EFFECT,
}
POSSIBLE_MISSING_MARKERS = frozenset({"na", "n/a", "null", "none", "nan", "."})
MEASUREMENT_ROLES = frozenset({ColumnRole.P_VALUE, ColumnRole.ADJUSTED_P_VALUE,
                               ColumnRole.EFFECT, ColumnRole.MEASUREMENT})
NUMERIC_TOKEN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ROWS = 1_000_000


class DatasetInspectionError(GeneDecisionError, ValueError):
    """A path/value-free structural error; row locators are safe to display."""

    def __init__(self, code: str, message: str, *, row_number: int | None = None):
        self.code = code
        self.row_number = row_number
        locator = f" (physical line {row_number})" if row_number is not None else ""
        super().__init__(f"{code}: {message}{locator}")


def _numeric_like(token: str) -> bool:
    if not NUMERIC_TOKEN.fullmatch(token):
        return False
    try:
        return Decimal(token).is_finite()
    except InvalidOperation:
        return False


def _strict_structure(text: str, delimiter: str, max_rows: int,
                      confirm_header: bool) -> tuple[tuple[str, ...], int]:
    """Add strict quote/blank-record checks missing from the legacy reader."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        header = next(reader, None)
        if header is None:
            raise DatasetInspectionError("EMPTY_FILE", "the file contains no header record")
        if not header or any(not name.strip() for name in header):
            raise DatasetInspectionError("MISSING_HEADER", "every column requires a header name")
        normalized = [name.strip() for name in header]
        if len(set(normalized)) != len(header):
            raise DatasetInspectionError("DUPLICATE_HEADER", "header names must be unique after whitespace stripping")
        if not confirm_header and any(_numeric_like(name.strip()) for name in header):
            raise DatasetInspectionError(
                "POSSIBLE_MISSING_HEADER",
                "the first record contains numeric header tokens; supply a header or explicitly confirm it",
            )
        count = blank_count = 0
        for row in reader:
            if not row:
                blank_count += 1
                continue
            if len(row) != len(header):
                raise DatasetInspectionError(
                    "MALFORMED_ROW_WIDTH", "record width does not match the header",
                    row_number=reader.line_num,
                )
            count += 1
            if count > max_rows:
                raise DatasetInspectionError("ROW_LIMIT", "table exceeds the configured row limit")
        if count == 0:
            raise DatasetInspectionError("EMPTY_TABLE", "the header has no data records")
        return tuple(header), blank_count
    except csv.Error:
        raise DatasetInspectionError("MALFORMED_QUOTING", "CSV/TSV quoting is invalid",
                                     row_number=reader.line_num) from None


def _inspect_table_with_rows(
    path: str | Path,
    *,
    mappings: Sequence[ColumnMapping] = (),
    dataset_type: DatasetType = DatasetType.UNKNOWN,
    input_format: str | None = None,
    encoding: str = "utf-8-sig",
    missing_tokens_by_column: Mapping[str, Sequence[str]] | None = None,
    confirm_header: bool = False,
    inspected_at: datetime | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> tuple[TableInspection, list[RawEvidenceRow]]:
    """Private snapshot helper: metadata and raw strings for local consumers.

    The suffix selects CSV/TSV unless ``input_format`` is explicitly supplied.
    Defaults recognize blank cells only. Nonblank NA-like spellings are merely
    reported as possible markers until declared by the caller for that column.
    A private temporary snapshot lets the unchanged ingestion utility parse
    exactly the bytes hashed here, without reopening a changing source file.
    """
    if encoding not in ("utf-8-sig", "utf-8", "utf-16"):
        raise DatasetInspectionError("UNSUPPORTED_ENCODING", "choose utf-8-sig, utf-8 or utf-16 explicitly")
    if type(max_bytes) is not int or max_bytes < 1 or type(max_rows) is not int or max_rows < 1:
        raise DatasetInspectionError("INVALID_LIMIT", "byte and row limits must be positive integers")
    if type(confirm_header) is not bool:
        raise DatasetInspectionError("INVALID_HEADER_CONFIRMATION", "confirm_header must be a boolean")
    source = Path(path)
    format_name = input_format.upper() if isinstance(input_format, str) else None
    if input_format is not None and format_name not in ("CSV", "TSV"):
        raise DatasetInspectionError("UNSUPPORTED_FORMAT", "only CSV and TSV are supported")
    format_name = format_name or {".csv": "CSV", ".tsv": "TSV"}.get(source.suffix.lower())
    if format_name is None:
        raise DatasetInspectionError("UNSUPPORTED_FORMAT", "use .csv/.tsv or explicitly declare CSV/TSV")
    delimiter = "," if format_name == "CSV" else "\t"
    try:
        if not source.is_file():
            raise DatasetInspectionError("FILE_UNAVAILABLE", "a readable local regular file is required")
        with source.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        raise DatasetInspectionError("FILE_UNAVAILABLE", "the selected file could not be read") from None
    if len(raw) > max_bytes:
        raise DatasetInspectionError("BYTE_LIMIT", "file exceeds the configured byte limit")
    try:
        text = raw.decode(encoding)
    except UnicodeError:
        raise DatasetInspectionError("INVALID_ENCODING", "bytes are invalid for the selected encoding") from None
    if "\x00" in text or "\ufeff" in text:
        raise DatasetInspectionError("INVALID_ENCODING", "decoded text contains NUL or an unhandled byte-order mark")
    header, blank_records = _strict_structure(text, delimiter, max_rows, confirm_header)

    # Existing reader owns raw-row parsing and locators, never normalization.
    try:
        with tempfile.TemporaryDirectory(prefix="genedecision-inspect-") as directory:
            snapshot = Path(directory) / ("snapshot.csv" if format_name == "CSV" else "snapshot.tsv")
            snapshot.write_bytes(raw)
            rows = read_evidence_rows(snapshot, encoding=encoding)
    except EvidenceImportError as exc:
        raise DatasetInspectionError("INVALID_TABLE", "the raw table reader rejected the structure",
                                     row_number=exc.row_number) from None
    except (OSError, UnicodeError, csv.Error):
        raise DatasetInspectionError("READ_FAILED", "the local snapshot could not be parsed") from None

    declared = tuple(ColumnMapping.model_validate(item) for item in mappings)
    mapped_names = [item.column for item in declared]
    if len(set(mapped_names)) != len(mapped_names) or not set(mapped_names) <= set(header):
        raise DatasetInspectionError("INVALID_MAPPING", "mappings must name unique actual header columns")
    declared = tuple(sorted(declared, key=lambda item: header.index(item.column)))
    by_name = {item.column: item for item in declared}
    policies = missing_tokens_by_column or {}
    if not set(policies) <= set(header):
        raise DatasetInspectionError("INVALID_MISSING_POLICY", "missing-value policy names an absent column")
    if any(isinstance(tokens, str) or not isinstance(tokens, Sequence)
           or any(not isinstance(token, str) for token in tokens) for tokens in policies.values()):
        raise DatasetInspectionError("INVALID_MISSING_POLICY", "missing tokens must be sequences of strings")
    profiles = []
    for name in header:
        role = HEADER_HINTS.get(name.strip().casefold(), ColumnRole.UNKNOWN)
        identifier = role is ColumnRole.GENE_IDENTIFIER or (
            name in by_name and by_name[name].role is ColumnRole.GENE_IDENTIFIER
        )
        tokens = tuple(sorted({token.strip().casefold() for token in policies.get(name, ()) if token.strip()}))
        blanks = missing = possible = numeric = 0
        for row in rows:
            value = row.values[name]
            # CSV DictReader returns text, including leading zeros and spaces.
            token = value.strip()
            if not token:
                blanks += 1
            elif token.casefold() in tokens:
                missing += 1
            else:
                if not identifier and token.casefold() in POSSIBLE_MISSING_MARKERS:
                    possible += 1
                if not identifier and _numeric_like(token):
                    numeric += 1
        profiles.append(ColumnInspection(
            column=name, tentative_role=role, blank_value_count=blanks,
            declared_missing_value_count=missing, possible_missing_marker_count=possible,
            numeric_like_value_count=None if identifier else numeric, missing_tokens=tokens,
        ))

    warnings: list[InspectionWarning] = []
    questions: list[MappingQuestion] = []
    if not confirm_header:
        questions.append(MappingQuestion(
            code="CONFIRM_HEADER",
            question="Confirm the first record contains header names; a headerless text-only file cannot be detected reliably.",
        ))
    genes = tuple(p.column for p in profiles if p.tentative_role is ColumnRole.GENE_IDENTIFIER)
    explicit_genes = tuple(m.column for m in declared if m.role is ColumnRole.GENE_IDENTIFIER)
    if not explicit_genes:
        questions.append(MappingQuestion(
            code="SELECT_GENE_COLUMN", columns=genes,
            question="Select the gene-identifier column and declare its namespace; header hints are suggestions only.",
        ))
        if not genes:
            warnings.append(InspectionWarning(code="NO_GENE_COLUMN_HINT", message="No recognized gene-column header; explicit mapping is needed later."))
        elif len(genes) > 1:
            warnings.append(InspectionWarning(code="AMBIGUOUS_GENE_COLUMNS", columns=genes, message="Several headers could identify genes; none was selected automatically."))
    else:
        questions.append(MappingQuestion(
            code="GENE_NAMESPACE_UNRESOLVED", columns=explicit_genes,
            question="Declare gene namespace and taxon before identifier resolution; this inspection validates neither.",
        ))
        if len(explicit_genes) > 1:
            questions.append(MappingQuestion(code="MULTIPLE_MAPPED_GENE_COLUMNS", columns=explicit_genes, question="Specify the primary identifier or the relationship between mapped identifier columns."))

    for profile in profiles:
        name = profile.column
        explicit = by_name.get(name)
        effective = explicit.role if explicit is not None else profile.tentative_role
        if effective in MEASUREMENT_ROLES:
            questions.append(MappingQuestion(
                code="MEASUREMENT_SEMANTICS_UNRESOLVED", columns=(name,),
                question="Confirm the measurement definition, analysis method, scale/contrast and any statistical adjustment; name or mapping alone validates none of them.",
            ))
        elif effective is ColumnRole.UNKNOWN:
            questions.append(MappingQuestion(code="COLUMN_ROLE_UNRESOLVED", columns=(name,), question="Declare the role of this column or explicitly retain it as other data; its scientific meaning is unknown."))
        if profile.blank_value_count or profile.declared_missing_value_count:
            warnings.append(InspectionWarning(code="MISSING_VALUES", columns=(name,), message="Blank or explicitly declared missing cells are retained as missing; no zero substitution is performed."))
        if profile.possible_missing_marker_count:
            questions.append(MappingQuestion(code="CONFIRM_MISSING_TOKENS", columns=(name,), question="Confirm which nonblank marker spellings represent missing values in this column."))
            warnings.append(InspectionWarning(code="POSSIBLE_MISSING_MARKERS", columns=(name,), message="Nonblank NA-like tokens were observed but are not automatically classified as missing."))
        if effective in MEASUREMENT_ROLES and profile.numeric_like_value_count is not None:
            other = (len(rows) - profile.blank_value_count - profile.declared_missing_value_count
                     - profile.possible_missing_marker_count - profile.numeric_like_value_count)
            if other:
                warnings.append(InspectionWarning(code="NON_NUMERIC_MEASUREMENT_TOKENS", columns=(name,), message="Some putative measurement cells are not finite numeric tokens; no values were repaired or interpreted."))
    if blank_records:
        warnings.append(InspectionWarning(code="BLANK_RECORDS", message="Completely blank records are not data rows; their count is recorded separately."))
    if len(header) == 1:
        warnings.append(InspectionWarning(code="SINGLE_COLUMN", message="One column was parsed; confirm the format if more columns were expected."))
    kind = DatasetType(dataset_type)
    timestamp = inspected_at if inspected_at is not None else datetime.now(timezone.utc)
    digest = hashlib.sha256(raw).hexdigest()
    payload = dict(
        schema_version="dataset-manifest-v1", dataset_id="DATASET_" + digest,
        source_file_sha256=digest, source_file_size=len(raw), source_scope="USER",
        input_format=format_name, delimiter=delimiter, encoding=encoding,
        row_count=len(rows), column_count=len(header), ordered_column_names=header,
        columns=tuple(profiles), explicit_mappings=declared,
        mapping_semantics="USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED",
        unresolved_mapping_questions=tuple(questions), dataset_type=kind,
        dataset_type_basis="NOT_ESTABLISHED" if kind is DatasetType.UNKNOWN else "USER_DECLARED",
        warnings=tuple(warnings), blank_record_count=blank_records,
        header_confirmed_by_caller=confirm_header,
        missing_token_matching="STRIPPED_CASE_INSENSITIVE",
    )
    # Use the same model serialization as the contract's identity validator.
    identity_data = {key: value for key, value in payload.items()}
    identity_data["columns"] = [item.model_dump(mode="json") for item in profiles]
    identity_data["explicit_mappings"] = [item.model_dump(mode="json") for item in declared]
    identity_data["unresolved_mapping_questions"] = [item.model_dump(mode="json") for item in questions]
    identity_data["warnings"] = [item.model_dump(mode="json") for item in warnings]
    manifest = DatasetManifest(
        **payload, inspected_at=timestamp,
        inspection_id="INSPECTION_" + DatasetManifest.content_digest(identity_data),
    )
    summary = "\n".join([
        f"{manifest.input_format}: {manifest.row_count} data rows, {manifest.column_count} columns.",
        "Columns: " + json_header(header),
        f"Dataset type: {manifest.dataset_type.value} ({manifest.dataset_type_basis}).",
        f"Source SHA-256: {digest}",
        "Gene-column suggestions: " + json_header(genes),
        *[f"Column {json_header((p.column,))}: blank={p.blank_value_count}, declared missing={p.declared_missing_value_count}, possible markers={p.possible_missing_marker_count}." for p in profiles],
        *[f"Warning [{w.code}]: {w.message}" for w in warnings],
        *[f"Unresolved [{q.code}] {json_header(q.columns)}: {q.question}" for q in questions],
        "Structural inspection only; no gene significance, category or priority was assessed.",
    ])
    inspection = TableInspection(manifest=manifest, summary=summary, row_locators=tuple(
        RowLocator(logical_record=index, physical_line_end=row.row_number)
        for index, row in enumerate(rows, start=1)
    ))
    return inspection, rows


def inspect_table(
    path: str | Path,
    *,
    mappings: Sequence[ColumnMapping] = (),
    dataset_type: DatasetType = DatasetType.UNKNOWN,
    input_format: str | None = None,
    encoding: str = "utf-8-sig",
    missing_tokens_by_column: Mapping[str, Sequence[str]] | None = None,
    confirm_header: bool = False,
    inspected_at: datetime | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> TableInspection:
    """Inspect one byte snapshot, returning metadata without raw rows (6B1)."""
    inspection, _ = _inspect_table_with_rows(
        path, mappings=mappings, dataset_type=dataset_type,
        input_format=input_format, encoding=encoding,
        missing_tokens_by_column=missing_tokens_by_column,
        confirm_header=confirm_header, inspected_at=inspected_at,
        max_bytes=max_bytes, max_rows=max_rows,
    )
    return inspection


def json_header(columns: tuple[str, ...]) -> str:
    """Quote header text safely, including tabs/newlines, without raw cells."""
    import json
    return json.dumps(columns, ensure_ascii=True)
