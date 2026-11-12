"""Normalization of raw evidence rows into the canonical ``EvidenceRecord``.

The normalizer only reshapes and type-checks what the user supplied:

* a missing value stays missing (``None``) and is never estimated;
* a categorical value must already be one of the schema's enum members, and is
  never case-folded, translated, or remapped onto a "closest" member;
* a supplied scientific number is stored exactly as parsed, never rounded,
  rescaled, or replaced.

Anything that cannot be normalized raises an :class:`EvidenceImportError` that
names the row and the field responsible.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import ValidationError

from genedecision.errors import DuplicateEvidenceError, EvidenceImportError
from genedecision.models.evidence import (
    Directness,
    EvidenceLevel,
    EvidenceRecord,
    SourceScope,
    Support,
)

#: Column names used by the user-evidence example in the specification (which
#: calls them ``dataset_name``, ``treatment``, and ``direction``) that differ
#: from the canonical unified-schema names used by ``EvidenceRecord``. Only the
#: *column name* is translated; the supplied *value* is never translated.
COLUMN_ALIASES: Mapping[str, str] = {
    "dataset_name": "source_name",
    "treatment": "drug",
    "direction": "support",
}

#: Cell contents that mean "the user did not supply a value". Matching is on the
#: whole cell (case-insensitive) after stripping surrounding whitespace.
MISSING_TOKENS: frozenset[str] = frozenset(
    {"", "na", "n/a", "null", "none", "nan", "nil", "."}
)

_ENUM_FIELDS: Mapping[str, type[Enum]] = {
    "source_scope": SourceScope,
    "evidence_level": EvidenceLevel,
    "support": Support,
    "directness": Directness,
}

_FLOAT_FIELDS: frozenset[str] = frozenset({"effect_value", "p_value", "fdr"})
#: Statistics whose physical domain is bounded. A value outside the bound cannot
#: be a real statistic, so it is rejected instead of being clipped or rescaled.
_PROBABILITY_FIELDS: frozenset[str] = frozenset({"p_value", "fdr"})
_NON_NEGATIVE_INT_FIELDS: frozenset[str] = frozenset({"sample_size"})


def _is_missing(raw: Any) -> bool:
    """Return ``True`` when the supplied cell means "no value"."""
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in MISSING_TOKENS
    return False


def _coerce_enum(
    name: str,
    enum_type: type[Enum],
    raw: Any,
    *,
    source: str | None,
    row_number: int | None,
) -> Enum:
    text = str(raw).strip()
    try:
        return enum_type(text)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise EvidenceImportError(
            f"invalid value for field {name!r}; expected exactly one of: {allowed}"
            " (values are matched exactly and are never remapped)",
            source=source,
            row_number=row_number,
            field=name,
            value=raw,
        ) from exc


def _coerce_float(
    name: str, raw: Any, *, source: str | None, row_number: int | None
) -> float:
    if isinstance(raw, bool):
        raise EvidenceImportError(
            f"field {name!r} expects a number but received a boolean",
            source=source,
            row_number=row_number,
            field=name,
            value=raw,
        )
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip()
        try:
            value = float(text)
        except ValueError as exc:
            raise EvidenceImportError(
                f"field {name!r} expects a number and could not be parsed;"
                " provide an exact value or leave the cell empty",
                source=source,
                row_number=row_number,
                field=name,
                value=raw,
            ) from exc
    if not math.isfinite(value):
        raise EvidenceImportError(
            f"field {name!r} must be a finite number",
            source=source,
            row_number=row_number,
            field=name,
            value=raw,
        )
    return value


def _coerce_probability(
    name: str, raw: Any, *, source: str | None, row_number: int | None
) -> float:
    value = _coerce_float(name, raw, source=source, row_number=row_number)
    if not 0.0 <= value <= 1.0:
        raise EvidenceImportError(
            f"field {name!r} must lie within [0, 1]; the supplied value was left"
            " untouched and rejected rather than adjusted",
            source=source,
            row_number=row_number,
            field=name,
            value=raw,
        )
    return value


def _coerce_int(
    name: str, raw: Any, *, source: str | None, row_number: int | None
) -> int:
    if isinstance(raw, bool):
        raise EvidenceImportError(
            f"field {name!r} expects an integer but received a boolean",
            source=source,
            row_number=row_number,
            field=name,
            value=raw,
        )
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        try:
            number = float(text)
        except ValueError as exc:
            raise EvidenceImportError(
                f"field {name!r} expects an integer and could not be parsed",
                source=source,
                row_number=row_number,
                field=name,
                value=raw,
            ) from exc
        if not math.isfinite(number) or not number.is_integer():
            raise EvidenceImportError(
                f"field {name!r} expects a whole number",
                source=source,
                row_number=row_number,
                field=name,
                value=raw,
            )
        value = int(number)
    if value < 0:
        raise EvidenceImportError(
            f"field {name!r} must be a non-negative integer",
            source=source,
            row_number=row_number,
            field=name,
            value=raw,
        )
    return value


def _coerce_datetime(
    name: str, raw: Any, *, source: str | None, row_number: int | None
) -> datetime:
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise EvidenceImportError(
            f"field {name!r} expects an ISO 8601 datetime",
            source=source,
            row_number=row_number,
            field=name,
            value=raw,
        ) from exc


def _coerce_quality_flags(
    name: str, raw: Any, *, source: str | None, row_number: int | None
) -> list[str]:
    if isinstance(raw, (list, tuple)):
        items: list[Any] = list(raw)
    else:
        text = str(raw).strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise EvidenceImportError(
                    f"field {name!r} looks like a JSON array but could not be parsed",
                    source=source,
                    row_number=row_number,
                    field=name,
                    value=raw,
                ) from exc
            if not isinstance(decoded, list):
                raise EvidenceImportError(
                    f"field {name!r} expects a list of strings",
                    source=source,
                    row_number=row_number,
                    field=name,
                    value=raw,
                )
            items = decoded
        else:
            items = text.split(";")

    flags: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise EvidenceImportError(
                f"field {name!r} expects string entries; received"
                f" {type(item).__name__}",
                source=source,
                row_number=row_number,
                field=name,
                value=item,
            )
        stripped = item.strip()
        if stripped:
            flags.append(stripped)
    return flags


def _coerce_value(
    name: str, raw: Any, *, source: str | None, row_number: int | None
) -> Any:
    if name in _ENUM_FIELDS:
        return _coerce_enum(
            name, _ENUM_FIELDS[name], raw, source=source, row_number=row_number
        )
    if name in _PROBABILITY_FIELDS:
        return _coerce_probability(name, raw, source=source, row_number=row_number)
    if name in _FLOAT_FIELDS:
        return _coerce_float(name, raw, source=source, row_number=row_number)
    if name in _NON_NEGATIVE_INT_FIELDS:
        return _coerce_int(name, raw, source=source, row_number=row_number)
    if name == "retrieved_at":
        return _coerce_datetime(name, raw, source=source, row_number=row_number)
    if name == "quality_flags":
        return _coerce_quality_flags(name, raw, source=source, row_number=row_number)
    return str(raw)


def _resolve_columns(
    row: Mapping[str, Any], *, source: str | None, row_number: int | None
) -> dict[str, Any]:
    """Translate source column names to canonical field names, keeping values."""
    canonical: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for raw_key, raw_value in row.items():
        if raw_key is None:
            raise EvidenceImportError(
                "row supplies more values than the header row defines",
                source=source,
                row_number=row_number,
            )
        key = str(raw_key).strip()
        if not key:
            raise EvidenceImportError(
                "row supplies a value under an unnamed column",
                source=source,
                row_number=row_number,
            )
        target = COLUMN_ALIASES.get(key, key)
        if target in canonical:
            raise EvidenceImportError(
                f"columns {origins[target]!r} and {key!r} both supply field"
                f" {target!r}; keep exactly one of them",
                source=source,
                row_number=row_number,
                field=target,
            )
        canonical[target] = raw_value
        origins[target] = key

    unknown = sorted(name for name in canonical if name not in EvidenceRecord.model_fields)
    if unknown:
        raise EvidenceImportError(
            "unrecognized field(s) "
            + ", ".join(repr(name) for name in unknown)
            + "; fields are not renamed or dropped silently",
            source=source,
            row_number=row_number,
            field=unknown[0],
        )
    return canonical


def normalize_evidence_row(
    row: Mapping[str, Any],
    *,
    row_number: int | None = None,
    source: str | None = None,
) -> EvidenceRecord:
    """Normalize one raw row into an :class:`EvidenceRecord`.

    Cells that are absent or marked as missing are omitted, so the schema's own
    defaults apply (including ``None`` for optional scientific values and
    ``UNASSESSED`` for directness). Raw ingestion does not infer directness.
    """
    canonical = _resolve_columns(row, source=source, row_number=row_number)

    data: dict[str, Any] = {}
    for name in EvidenceRecord.model_fields:
        if name not in canonical:
            continue
        raw = canonical[name]
        if _is_missing(raw):
            continue
        data[name] = _coerce_value(
            name, raw, source=source, row_number=row_number
        )

    try:
        return EvidenceRecord(**data)
    except ValidationError as exc:
        problems = exc.errors()
        first = problems[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "<record>"
        details = "; ".join(
            f"{'.'.join(str(part) for part in problem.get('loc', ())) or '<record>'}:"
            f" {problem.get('msg', '')}"
            for problem in problems
        )
        raise EvidenceImportError(
            f"record violates the EvidenceRecord schema: {details}",
            source=source,
            row_number=row_number,
            field=location,
        ) from exc


def normalize_evidence_rows(
    rows: Iterable[tuple[int, Mapping[str, Any]]],
    *,
    source: str | None = None,
) -> list[EvidenceRecord]:
    """Normalize rows in input order, rejecting repeated ``evidence_id`` values.

    Ordering is the order of the source file, and a repeated ``evidence_id`` is
    reported together with both row numbers instead of one record silently
    overwriting the other.
    """
    records: list[EvidenceRecord] = []
    seen_at: dict[str, int] = {}
    for row_number, row in rows:
        record = normalize_evidence_row(
            row, row_number=row_number, source=source
        )
        if record.evidence_id in seen_at:
            raise DuplicateEvidenceError(
                f"duplicate evidence_id {record.evidence_id!r}: already supplied"
                f" at row {seen_at[record.evidence_id]} and supplied again at row"
                f" {row_number}",
                source=source,
                row_number=row_number,
                field="evidence_id",
                value=record.evidence_id,
            )
        seen_at[record.evidence_id] = row_number
        records.append(record)
    return records
