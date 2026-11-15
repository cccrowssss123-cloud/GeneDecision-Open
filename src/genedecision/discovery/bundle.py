"""Local Discovery artifacts with a manifest published only after all files exist."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Declaration, Digest, Text
from .identifiers import _declaration_json
from .models import DatasetManifest
from .run import DiscoveryRun
from .tables import DatasetInspectionError

_FILES = (
    "observations.jsonl", "row_outcomes.jsonl", "candidates.jsonl",
    "candidates.tsv", "requirements.json",
)
_MANIFEST = "run_manifest.json"
_PENDING = ".run_manifest.pending"
_MANIFEST_LIMIT = 1024 * 1024
_WARNING = "SCIENTIFICALLY_UNVERIFIED"
_CLASSIFICATION = "LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"
Count = Annotated[int, Field(strict=True, ge=0)]


class BundleError(DatasetInspectionError):
    def __init__(self, code: str):
        super().__init__(code, "the local Discovery bundle operation could not complete")


class ArtifactDigest(Declaration):
    sha256: Digest
    size_bytes: Count


class BundleManifest(Declaration):
    schema_version: Literal["candidate-discovery-bundle-v1"] = "candidate-discovery-bundle-v1"
    data_classification: Literal["LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"] = _CLASSIFICATION
    warning: Literal["SCIENTIFICALLY_UNVERIFIED"] = _WARNING
    scientific_validation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    software_version: Text
    run_id: Text
    dataset_id: Text
    source_sha256: Digest
    source_size_bytes: Count
    row_count: Count
    candidate_count: Count
    numeric_qc_flagged_rows: Count | None
    row_truth_counts: dict[Literal["T", "F", "U"], Count]
    files: dict[str, ArtifactDigest]
    bundle_id: Digest

    @model_validator(mode="after")
    def check_manifest(self) -> BundleManifest:
        if set(self.files) != set(_FILES):
            raise ValueError("BUNDLE_FILE_SET_MISMATCH")
        if set(self.row_truth_counts) != {"T", "F", "U"} or sum(self.row_truth_counts.values()) != self.row_count:
            raise ValueError("BUNDLE_ROW_COUNTS_MISMATCH")
        if self.candidate_count > self.row_truth_counts["T"]:
            raise ValueError("BUNDLE_CANDIDATE_COUNT_MISMATCH")
        if self.numeric_qc_flagged_rows is not None and self.numeric_qc_flagged_rows > self.row_count:
            raise ValueError("BUNDLE_QC_COUNT_MISMATCH")
        if self.bundle_id != DatasetManifest.content_digest(self.model_dump(mode="json", exclude={"bundle_id"})):
            raise ValueError("BUNDLE_ID_MISMATCH")
        return self


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json_line(value: object) -> bytes:
    return (_json(value) + "\n").encode("utf-8")


def _dump(value, **options):
    return value.model_dump(mode="json", **options) if value is not None else None


def _observations(run: DiscoveryRun):
    manifest = run.identifiers.inspection.manifest
    audit = run.numeric_evaluation.measurement_audit if run.numeric_evaluation is not None else None
    for index, row in enumerate(run.observations):
        yield _json_line({
            "locator": _dump(row.locator),
            "identifier": _dump(run.identifiers.identifiers[index]),
            "raw_values": row.raw_cells,
            "column_names": manifest.ordered_column_names,
            "measurement_cells": [_dump(cell) for cell in audit.rows[index].cells] if audit is not None else None,
            "warning": _WARNING,
        })


def _outcomes(run: DiscoveryRun):
    for index, row in enumerate(run.observations):
        category = run.category_evaluation.rows[index] if run.category_evaluation is not None else None
        numeric = run.numeric_evaluation.rows[index] if run.numeric_evaluation is not None else None
        yield _json_line({
            "locator": _dump(row.locator),
            "canonical_key": _dump(row.canonical_key),
            "truth": row.truth,
            "category": _dump(category, exclude={"identifier_record"}),
            "numeric": _dump(numeric, exclude={"identifier_record"}),
            "warning": _WARNING,
        })


def _candidate_entries(run: DiscoveryRun):
    for candidate in run.candidates:
        yield {
            "key": _dump(candidate.key),
            "matching_rows": [_dump(locator) for locator in candidate.matching_rows],
            "observed_rows": [_dump(locator) for locator in candidate.observed_rows],
            "interpretation_preparation": "NOT_ASSESSED",
            "decision_preparation": "REQUIRES_CONFIRMED_HYPOTHESIS_AND_SUCCESSFUL_INTERPRETATIONS",
            "warning": _WARNING,
        }


def _tsv_text(text: str) -> str:
    # Encode dangerous prefixes and existing apostrophes so the TSV rule is reversible.
    leading = text.lstrip().lstrip("\ufeff").lstrip()
    if leading.startswith(("=", "+", "-", "@", "'")):
        return "'" + text
    return text


def _tsv(run: DiscoveryRun):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\r\n")

    def record(columns) -> bytes:
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(columns)
        return buffer.getvalue().encode("utf-8")

    yield record((
        "taxon", "namespace", "identifier", "logical_record", "physical_line_end",
        "category_truth", "numeric_truth", "declared_qc_failure", "row_trace_file",
        "warning", "interpretation_preparation",
    ))
    for candidate in run.candidates:
        key = candidate.key
        for locator in candidate.matching_rows:
            index = locator.logical_record - 1
            category = run.category_evaluation
            numeric = run.numeric_evaluation
            yield record((
                _tsv_text(key.taxon), _tsv_text(key.namespace), _tsv_text(key.identifier),
                locator.logical_record, locator.physical_line_end,
                category.rows[index].truth if category is not None else "",
                numeric.rows[index].truth if numeric is not None else "",
                str(numeric.rows[index].has_declared_qc_failure).lower() if numeric is not None else "",
                "row_outcomes.jsonl", _WARNING, "NOT_ASSESSED",
            ))


def _requirements(run: DiscoveryRun) -> dict:
    ids = run.identifiers
    category = run.category_evaluation
    numeric = run.numeric_evaluation
    return {
        "dataset_manifest": _dump(ids.inspection.manifest, exclude={"inspected_at"}),
        "identifier_processing_id": ids.processing_id,
        "column_contract": _dump(ids.column_contract),
        "column_contract_sha256": ids.contract_sha256,
        "mapping_reference_supplied": ids.reference_supplied,
        "combine": run.combine,
        "category": {
            "evaluation_id": category.evaluation_id,
            "reference": _dump(category.annotation_binding),
            "condition": _dump(category.conditions),
        } if category is not None else None,
        "numeric": {
            "evaluation_id": numeric.evaluation_id,
            "audit_id": numeric.measurement_audit.audit_id,
            "audit_contract": _dump(numeric.measurement_audit.audit_contract),
            "condition": _dump(numeric.condition),
            "context": _dump(numeric.context),
        } if numeric is not None else None,
        "warning": _WARNING,
        "scientific_validation": "NOT_PERFORMED",
        "data_classification": _CLASSIFICATION,
    }


def _write(path: Path, chunks, created: list[Path]) -> ArtifactDigest:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    created.append(path)
    with os.fdopen(descriptor, "wb") as handle:
        for chunk in chunks:
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    return ArtifactDigest(sha256=digest.hexdigest(), size_bytes=size)


def _build_manifest(run: DiscoveryRun, files: dict[str, ArtifactDigest]) -> BundleManifest:
    try:
        software_version = version("genedecision")
    except PackageNotFoundError:
        software_version = "SOURCE_CHECKOUT"
    dataset = run.identifiers.inspection.manifest
    counts = Counter(row.truth for row in run.observations)
    numeric = run.numeric_evaluation
    payload = BundleManifest.model_construct(
        software_version=software_version, run_id=run.run_id,
        dataset_id=dataset.dataset_id, source_sha256=dataset.source_file_sha256,
        source_size_bytes=dataset.source_file_size, row_count=len(run.observations),
        candidate_count=len(run.candidates), row_truth_counts={key: counts[key] for key in ("T", "F", "U")},
        numeric_qc_flagged_rows=sum(row.has_declared_qc_failure for row in numeric.rows) if numeric is not None else None,
        files=files, bundle_id="pending",
    ).model_dump(mode="json", exclude={"bundle_id"})
    return BundleManifest(**payload, bundle_id=DatasetManifest.content_digest(payload))


def write_discovery_bundle(run: DiscoveryRun, destination: str | Path) -> BundleManifest:
    """Write a new private directory. A manifest is the atomic completion marker."""
    try:
        run = DiscoveryRun.model_validate(run.model_dump(mode="python", warnings=False))
    except (AttributeError, ValueError, TypeError, RecursionError):
        raise BundleError("INVALID_DISCOVERY_RUN") from None
    path = Path(destination)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise BundleError("BUNDLE_DESTINATION_EXISTS") from None
    except (OSError, ValueError):
        raise BundleError("BUNDLE_DESTINATION_UNAVAILABLE") from None

    created: list[Path] = []
    completed = False
    try:
        files = {
            "observations.jsonl": _write(path / "observations.jsonl", _observations(run), created),
            "row_outcomes.jsonl": _write(path / "row_outcomes.jsonl", _outcomes(run), created),
            "candidates.jsonl": _write(path / "candidates.jsonl", map(_json_line, _candidate_entries(run)), created),
            "candidates.tsv": _write(path / "candidates.tsv", _tsv(run), created),
            "requirements.json": _write(path / "requirements.json", [_json_line(_requirements(run))], created),
        }
        manifest = _build_manifest(run, files)
        _write(path / _PENDING, [_json_line(_dump(manifest))], created)
        os.replace(path / _PENDING, path / _MANIFEST)
        completed = True
        return manifest
    except (OSError, ValueError, TypeError, RecursionError):
        raise BundleError("BUNDLE_WRITE_FAILED") from None
    finally:
        if not completed:
            for file in reversed(created):
                try:
                    file.unlink()
                except OSError:
                    pass
            try:
                path.rmdir()
            except OSError:
                pass


def _open_regular(path: Path):
    if path.is_symlink():
        raise OSError("bundle file is a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("bundle file is not regular")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def verify_discovery_bundle(directory: str | Path) -> BundleManifest:
    """Verify a complete bundle's structure and checksums, without reading source data."""
    path = Path(directory)
    try:
        if path.is_symlink():
            raise OSError("bundle directory is a symlink")
        with _open_regular(path / _MANIFEST) as handle:
            raw = handle.read(_MANIFEST_LIMIT + 1)
        if len(raw) > _MANIFEST_LIMIT:
            raise ValueError("manifest too large")
        manifest = BundleManifest.model_validate(_declaration_json(raw))
        if {entry.name for entry in path.iterdir()} != {_MANIFEST, *_FILES}:
            raise ValueError("bundle file set mismatch")
        for name in _FILES:
            expected = manifest.files[name]
            digest = hashlib.sha256()
            size = 0
            with _open_regular(path / name) as handle:
                if os.fstat(handle.fileno()).st_size != expected.size_bytes:
                    raise ValueError("artifact size mismatch")
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > expected.size_bytes:
                        raise ValueError("artifact size mismatch")
                    digest.update(chunk)
            if size != expected.size_bytes or digest.hexdigest() != expected.sha256:
                raise ValueError("artifact digest mismatch")
        return manifest
    except (OSError, ValueError, TypeError, RecursionError):
        raise BundleError("BUNDLE_INVALID_OR_INCOMPLETE") from None
