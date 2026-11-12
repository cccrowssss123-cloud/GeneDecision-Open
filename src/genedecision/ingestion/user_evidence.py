"""Read user-supplied evidence files into raw, un-interpreted rows.

This module owns file-format concerns only. It never decides what a value means:
it produces :class:`RawEvidenceRow` values and hands them to
:mod:`genedecision.normalization.evidence_normalizer` for schema validation.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from genedecision.errors import (
    EvidenceImportError,
    UnsupportedEvidenceFormatError,
)
from genedecision.models.evidence import EvidenceRecord
from genedecision.normalization.evidence_normalizer import normalize_evidence_rows

#: ``utf-8-sig`` transparently drops a byte-order mark when one is present and
#: is otherwise identical to UTF-8, so Excel-exported CSV/TSV files load intact.
DEFAULT_ENCODING = "utf-8-sig"

DELIMITED_SUFFIXES: Mapping[str, str] = {".csv": ",", ".tsv": "\t", ".tab": "\t"}
JSON_SUFFIXES: frozenset[str] = frozenset({".json"})

#: Top-level JSON keys that may hold the list of evidence objects. An envelope is
#: recognized only when exactly one of them is present.
JSON_ENVELOPE_KEYS: tuple[str, ...] = ("evidence", "evidence_records", "records")


class RawEvidenceRow(NamedTuple):
    """One row of user evidence, before any schema interpretation.

    ``row_number`` is a locator inside the source file: the physical line number
    for CSV/TSV, and the 1-based element index for JSON.
    """

    row_number: int
    values: Mapping[str, Any]


def read_evidence_rows(
    path: str | Path,
    *,
    delimiter: str | None = None,
    encoding: str = DEFAULT_ENCODING,
) -> list[RawEvidenceRow]:
    """Read an evidence file into raw rows, dispatching on the file extension."""
    source_path = Path(path)
    source = str(source_path)
    if not source_path.is_file():
        raise EvidenceImportError(
            "evidence file was not found; provide a CSV, TSV, or JSON file",
            source=source,
        )

    suffix = source_path.suffix.lower()
    if suffix in JSON_SUFFIXES:
        return _read_json_rows(source_path, encoding=encoding, source=source)
    if suffix in DELIMITED_SUFFIXES:
        delimiter = delimiter if delimiter is not None else DELIMITED_SUFFIXES[suffix]
        if len(delimiter) != 1:
            raise EvidenceImportError(
                "delimiter must be exactly one character",
                source=source,
                value=delimiter,
            )
        return _read_delimited_rows(
            source_path, delimiter=delimiter, encoding=encoding, source=source
        )

    supported = ", ".join(sorted(set(DELIMITED_SUFFIXES) | JSON_SUFFIXES))
    raise UnsupportedEvidenceFormatError(
        f"unsupported evidence file extension {suffix or '<none>'!r};"
        f" supported extensions are: {supported}",
        source=source,
    )


def load_evidence_records(
    path: str | Path,
    *,
    delimiter: str | None = None,
    encoding: str = DEFAULT_ENCODING,
) -> list[EvidenceRecord]:
    """Read and normalize an evidence file into ``EvidenceRecord`` objects."""
    source = str(Path(path))
    rows = read_evidence_rows(path, delimiter=delimiter, encoding=encoding)
    return normalize_evidence_rows(rows, source=source)


def _read_delimited_rows(
    path: Path, *, delimiter: str, encoding: str, source: str
) -> list[RawEvidenceRow]:
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        header = reader.fieldnames
        if header is None:
            raise EvidenceImportError(
                "file has no header row; the first line must name the columns",
                source=source,
            )
        _validate_header(header, source=source)

        rows = []
        for raw_row in reader:
            line_number = reader.line_num
            # csv.DictReader pads a short row with ``None`` and collects a long
            # row under the key ``None``. Either one would shift values into the
            # wrong columns, so both are refused rather than reinterpreted.
            short = [
                name
                for name, value in raw_row.items()
                if name is not None and value is None
            ]
            if short:
                raise EvidenceImportError(
                    "row supplies fewer values than the header row defines;"
                    " missing value(s) for column(s): "
                    + ", ".join(repr(name) for name in short),
                    source=source,
                    row_number=line_number,
                )
            if None in raw_row:
                raise EvidenceImportError(
                    "row supplies more values than the header row defines",
                    source=source,
                    row_number=line_number,
                )
            rows.append(
                RawEvidenceRow(row_number=line_number, values=dict(raw_row))
            )

    if not rows:
        raise EvidenceImportError(
            "file contains a header but no evidence rows",
            source=source,
        )
    return rows


def _validate_header(header: list[str], *, source: str) -> None:
    normalized = [name.strip() if name else "" for name in header]

    unnamed = [index + 1 for index, name in enumerate(normalized) if not name]
    if unnamed:
        positions = ", ".join(str(position) for position in unnamed)
        raise EvidenceImportError(
            f"header defines an unnamed column at position(s) {positions}",
            source=source,
        )

    duplicated = sorted({name for name in normalized if normalized.count(name) > 1})
    if duplicated:
        raise EvidenceImportError(
            "header defines the same column more than once: "
            + ", ".join(repr(name) for name in duplicated)
            + "; duplicate columns would make the mapping ambiguous",
            source=source,
        )


def _read_json_rows(path: Path, *, encoding: str, source: str) -> list[RawEvidenceRow]:
    try:
        payload = json.loads(path.read_text(encoding=encoding))
    except json.JSONDecodeError as exc:
        raise EvidenceImportError(
            f"file is not valid JSON: {exc.msg} (line {exc.lineno},"
            f" column {exc.colno})",
            source=source,
        ) from exc

    items = _json_items(payload, source=source)

    rows: list[RawEvidenceRow] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise EvidenceImportError(
                "each evidence entry must be a JSON object, received"
                f" {type(item).__name__}",
                source=source,
                row_number=index,
            )
        rows.append(RawEvidenceRow(row_number=index, values=item))

    if not rows:
        raise EvidenceImportError(
            "file contains no evidence entries", source=source
        )
    return rows


def _json_items(payload: Any, *, source: str) -> list[Any]:
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        present = [key for key in JSON_ENVELOPE_KEYS if key in payload]
        if len(present) > 1:
            raise EvidenceImportError(
                "JSON envelope is ambiguous; it holds more than one evidence"
                " list: " + ", ".join(repr(key) for key in present),
                source=source,
            )
        if present:
            candidate = payload[present[0]]
            if not isinstance(candidate, list):
                raise EvidenceImportError(
                    f"JSON key {present[0]!r} must hold a list of evidence"
                    f" objects, received {type(candidate).__name__}",
                    source=source,
                )
            return candidate
        return [payload]

    raise EvidenceImportError(
        "JSON evidence file must hold an object or a list of objects, received"
        f" {type(payload).__name__}",
        source=source,
    )
