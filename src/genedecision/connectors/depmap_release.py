"""Local, release-pinned DepMap release files: identity, acquisition, parsing.

Three things are deliberately kept apart here, because the DepMap access
contract makes them different:

1. **Manifest discovery** establishes which releases and datasets DepMap
   officially publishes. It is metadata, not a file download; v0.1 reads a
   caller-saved official manifest and performs no network discovery itself.
2. **Release-file acquisition** is a *local* operation in v0.1: the caller
   provides an official release directory, and this module resolves the exact
   filenames inside it. No unattended remote release-file download is
   implemented, no portal page is scraped, and browser verification is never
   bypassed.
3. **Parsing** reads the selected files only. Expression, mutation, copy
   number, PRISM and drug-response modalities are not selected and are never
   read.

Release identity plus dataset/file identity is the durable provenance. A
download URL carrying query parameters is a transient transport detail, so it
is never stored as the source identifier — ``official_file_identity`` is.

Nothing in this module interprets the data. It locates columns, parses the
documented ``SYMBOL (ENTREZ)`` header form, validates structure, and returns
source values verbatim.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from genedecision.connectors.base import ConnectorFailureKind
from genedecision.models.depmap import (
    CommonEssentialAnnotationType,
    DepMapFailureReason,
    DepMapMeasurementType,
    GeneResolutionBasis,
)

#: Official DepMap downloadable-file manifest. Metadata only.
DEPMAP_MANIFEST_URL = "https://depmap.org/portal/api/no-captcha/download/files"

#: Hash algorithm used for every locally computed integrity value.
LOCAL_INTEGRITY_ALGORITHM = "SHA-256"

MANIFEST_COLUMNS: tuple[str, ...] = (
    "release",
    "release_date",
    "filename",
    "url",
    "md5_hash",
)
MANIFEST_REQUIRED_COLUMNS: tuple[str, ...] = ("release", "filename")

#: The exact header cell DepMap matrices use for their first column.
MODEL_ID_COLUMN = "ModelID"

#: ``Model.csv`` column names, matched case-insensitively.
MODEL_ID_FIELD = "modelid"
MODEL_OPTIONAL_FIELDS: Mapping[str, str] = {
    "celllinename": "cell_line_name",
    "depmapmodeltype": "depmap_model_type",
    "oncotreelineage": "oncotree_lineage",
    "oncotreeprimarydisease": "oncotree_primary_disease",
    "oncotreesubtype": "oncotree_subtype",
}

#: Column names accepted in ``Gene.csv`` when it is not a single combined
#: identity column. A header that matches none of them is refused rather than
#: guessed at.
_GENE_SYMBOL_FIELD_NAMES: frozenset[str] = frozenset(
    {"gene", "genes", "symbol", "gene_name", "genename", "hugo_symbol", "gene_symbol"}
)
_GENE_ENTREZ_FIELD_NAMES: frozenset[str] = frozenset(
    {"entrez_id", "entrezid", "entrez_gene_id", "entrez", "gene_id", "geneid"}
)

#: Header tokens accepted as a header row in a single-column gene-set file.
_GENE_SET_HEADER_NAMES: frozenset[str] = (
    _GENE_SYMBOL_FIELD_NAMES | _GENE_ENTREZ_FIELD_NAMES
)

#: Empty/NA-shaped tokens a CRISPR matrix uses for an explicitly missing cell.
#: ``""`` means the cell exists but is blank; the rest are the source's own
#: missing-value spellings. None of them is ever converted to zero.
MISSING_CELL_TOKENS: frozenset[str] = frozenset(
    {"", "na", "n/a", "nan", "null", "none", "."}
)

#: The documented matrix gene-column form: ``SYMBOL (ENTREZ)``, fully anchored.
_MATRIX_GENE_HEADER = re.compile(r"^(?P<symbol>[^\s()]+)\s*\((?P<entrez>[0-9]+)\)$")

#: The documented release-code form, e.g. the ``26Q1`` in ``DepMap Public 26Q1``.
_RELEASE_CODE = re.compile(r"^[0-9]{2}Q[1-4]$")


class DepMapCollectionError(Exception):
    """An expected DepMap failure, carried into a ``FAILURE`` result.

    ``reason`` is the precise DepMap cause and ``kind`` is the coarse
    connector-facing classification; they are kept separate so a
    source-checksum conflict is never described as a local-integrity change (or
    the reverse). Programming errors are not raised through this type.
    """

    def __init__(
        self,
        reason: DepMapFailureReason,
        message: str,
        *,
        kind: ConnectorFailureKind | None = None,
        http_status: int | None = None,
    ) -> None:
        self.reason = reason
        self.message = message
        self.kind = kind if kind is not None else FAILURE_KINDS[reason]
        self.http_status = http_status
        super().__init__(message)


#: Coarse classification for each DepMap-specific cause. ``MISSING_RELEASE_FILE``
#: and ``RELEASE_NOT_FOUND`` are query rejections (the selected bundle could not
#: answer the question), never transport outages; the precise cause stays in
#: ``reason``.
FAILURE_KINDS: Mapping[DepMapFailureReason, ConnectorFailureKind] = {
    DepMapFailureReason.INVALID_QUERY: ConnectorFailureKind.INVALID_QUERY,
    DepMapFailureReason.MANIFEST_UNAVAILABLE: ConnectorFailureKind.TRANSPORT,
    DepMapFailureReason.MANIFEST_MALFORMED: ConnectorFailureKind.MALFORMED_RESPONSE,
    DepMapFailureReason.RELEASE_NOT_FOUND: ConnectorFailureKind.QUERY_REJECTED,
    DepMapFailureReason.MISSING_RELEASE_FILE: ConnectorFailureKind.QUERY_REJECTED,
    DepMapFailureReason.SOURCE_CHECKSUM_MISMATCH: (
        ConnectorFailureKind.MALFORMED_RESPONSE
    ),
    DepMapFailureReason.LOCAL_INTEGRITY_MISMATCH: (
        ConnectorFailureKind.MALFORMED_RESPONSE
    ),
    DepMapFailureReason.MALFORMED_DATASET_FILE: ConnectorFailureKind.MALFORMED_RESPONSE,
    DepMapFailureReason.INCONSISTENT_MATRIX: ConnectorFailureKind.MALFORMED_RESPONSE,
    DepMapFailureReason.INCONSISTENT_GENE_IDENTITY: (
        ConnectorFailureKind.MALFORMED_RESPONSE
    ),
    DepMapFailureReason.GENE_IDENTITY_AMBIGUOUS: ConnectorFailureKind.INVALID_QUERY,
}


class DepMapDataset(StrEnum):
    """The selected release files, by their stable DepMap dataset names.

    ``CRISPRScreenMap``, expression, mutation, copy number, PRISM and drug
    response files are deliberately absent: the integrated model-level CRISPR
    matrices already carry ``ModelID`` rows, and the other modalities are out of
    scope for Milestone 3B.
    """

    README = "README"
    CRISPR_GENE_EFFECT = "CRISPRGeneEffect"
    CRISPR_GENE_DEPENDENCY = "CRISPRGeneDependency"
    CRISPR_INFERRED_COMMON_ESSENTIALS = "CRISPRInferredCommonEssentials"
    ACHILLES_COMMON_ESSENTIAL_CONTROLS = "AchillesCommonEssentialControls"
    ACHILLES_NONESSENTIAL_CONTROLS = "AchillesNonessentialControls"
    GENE = "Gene"
    MODEL = "Model"

    @property
    def filename(self) -> str:
        """The exact official filename for this dataset."""
        return "README.txt" if self is DepMapDataset.README else f"{self.value}.csv"

    @property
    def measurement_types(self) -> frozenset[DepMapMeasurementType]:
        """The measurement types this dataset may emit.

        ``CRISPRGeneEffect`` emits only ``GENE_EFFECT`` and
        ``CRISPRGeneDependency`` only ``DEPENDENCY_PROBABILITY``. A requested
        pairing outside this mapping is a source-contract violation, not a
        value to reinterpret.
        """
        if self is DepMapDataset.CRISPR_GENE_EFFECT:
            return frozenset({DepMapMeasurementType.GENE_EFFECT})
        if self is DepMapDataset.CRISPR_GENE_DEPENDENCY:
            return frozenset({DepMapMeasurementType.DEPENDENCY_PROBABILITY})
        return frozenset()

    @property
    def annotation_type(self) -> CommonEssentialAnnotationType | None:
        """The source-defined essentiality concept this file defines, if any."""
        return _ANNOTATION_TYPES.get(self)


_ANNOTATION_TYPES: Mapping[DepMapDataset, CommonEssentialAnnotationType] = {
    DepMapDataset.CRISPR_INFERRED_COMMON_ESSENTIALS: (
        CommonEssentialAnnotationType.CRISPR_INFERRED_COMMON_ESSENTIAL
    ),
    DepMapDataset.ACHILLES_COMMON_ESSENTIAL_CONTROLS: (
        CommonEssentialAnnotationType.REFERENCE_ESSENTIAL_CONTROL
    ),
    DepMapDataset.ACHILLES_NONESSENTIAL_CONTROLS: (
        CommonEssentialAnnotationType.REFERENCE_NONESSENTIAL_CONTROL
    ),
}

#: Every file the milestone requires, in a fixed order.
REQUIRED_DATASETS: tuple[DepMapDataset, ...] = (
    DepMapDataset.README,
    DepMapDataset.CRISPR_GENE_EFFECT,
    DepMapDataset.CRISPR_GENE_DEPENDENCY,
    DepMapDataset.CRISPR_INFERRED_COMMON_ESSENTIALS,
    DepMapDataset.ACHILLES_COMMON_ESSENTIAL_CONTROLS,
    DepMapDataset.ACHILLES_NONESSENTIAL_CONTROLS,
    DepMapDataset.MODEL,
    DepMapDataset.GENE,
)

MATRIX_DATASETS: tuple[DepMapDataset, ...] = (
    DepMapDataset.CRISPR_GENE_EFFECT,
    DepMapDataset.CRISPR_GENE_DEPENDENCY,
)

GENE_SET_DATASETS: tuple[DepMapDataset, ...] = (
    DepMapDataset.CRISPR_INFERRED_COMMON_ESSENTIALS,
    DepMapDataset.ACHILLES_COMMON_ESSENTIAL_CONTROLS,
    DepMapDataset.ACHILLES_NONESSENTIAL_CONTROLS,
)

#: The source's own definition of each essentiality concept, preserved verbatim
#: as a factual label. Nothing here says what membership *means* for a user's
#: hypothesis.
SOURCE_DEFINITIONS: Mapping[CommonEssentialAnnotationType, str] = {
    CommonEssentialAnnotationType.CRISPR_INFERRED_COMMON_ESSENTIAL: (
        "Post-Chronos, screen-derived genes identified by DepMap from the"
        " release's integrated CRISPR gene-effect data; DepMap describes the"
        " pipeline as identifying pan-dependent genes using the"
        " 90th-percentile least-dependent line and a data-derived rank cutoff."
    ),
    CommonEssentialAnnotationType.REFERENCE_ESSENTIAL_CONTROL: (
        "Pre-Chronos reference positive-control set: the intersection of the"
        " Blomen and Hart essential sets, used by the pipeline for scaling and"
        " as the dependent reference distribution in dependency-probability"
        " inference."
    ),
    CommonEssentialAnnotationType.REFERENCE_NONESSENTIAL_CONTROL: (
        "Pre-Chronos Hart nonessential control set, used by the pipeline as a"
        " negative/null reference where applicable."
    ),
}


# ---------------------------------------------------------------------------
# identity helpers
# ---------------------------------------------------------------------------


def normalize_release_name(release_name: str) -> str:
    """Collapse outer and inner whitespace; the input is otherwise unchanged."""
    return " ".join(release_name.split())


def derive_release_code(release_name: str) -> str | None:
    """The release's own quarter code, e.g. ``26Q1``, when it has that form.

    The final whitespace-separated token is used, so ``DepMap Public 26Q1``
    yields ``26Q1``. A release whose name does not end in that form gets no code
    rather than a guessed one.
    """
    tokens = normalize_release_name(release_name).split(" ")
    if tokens and _RELEASE_CODE.match(tokens[-1]):
        return tokens[-1]
    return None


def release_identity_token(release_name: str) -> str:
    """A deterministic, readable token that identifies one release.

    The release code is used when the name carries one. Otherwise a short digest
    of the normalized name is used, so two differently named releases can never
    collapse onto the same identity.
    """
    code = derive_release_code(release_name)
    if code is not None:
        return code
    digest = hashlib.sha256(
        normalize_release_name(release_name).encode("utf-8")
    ).hexdigest()
    return f"R{digest[:10].upper()}"


def depmap_release_id(release_name: str) -> str:
    """Stable local identifier for onerelease identity."""
    return f"DEPMAP_{release_identity_token(release_name)}"


def depmap_evidence_id(release_name: str, gene_identifier: str) -> str:
    """Profile-level evidence identity: one per gene × DepMap release.

    Deterministic and collision-safe across releases, because the release token
    is either the release's own code or a digest of its full name.
    """
    return f"{depmap_release_id(release_name)}_CRISPR_{gene_identifier}"


def depmap_dataset_file_id(release_name: str, dataset: DepMapDataset) -> str:
    """Stable key for release + dataset/file identity."""
    return f"{depmap_release_id(release_name)}:{dataset.value}"


def depmap_official_file_identity(
    release_name: str, dataset: DepMapDataset
) -> str:
    """Release + dataset name + exact filename; provenance without a URL."""
    return f"{normalize_release_name(release_name)}:{dataset.value}:{dataset.filename}"


def depmap_observation_id(
    release_name: str,
    dataset: DepMapDataset,
    model_id: str,
    symbol: str | None,
    entrez_gene_id: str | None,
    measurement_type: DepMapMeasurementType,
) -> str:
    """Deterministic observation identity.

    Built from release, source-file identity, ``ModelID``, the gene's source
    identity and the measurement type — the same components the relational
    uniqueness constraint uses, so a re-import upserts instead of duplicating.
    The gene token is prefixed with ``E``/``S`` so an Entrez ID can never
    collide with a symbol of the same text.
    """
    gene_token = (
        f"E{entrez_gene_id}"
        if entrez_gene_id is not None
        else f"S{(symbol or '').casefold()}"
    )
    return (
        f"{depmap_dataset_file_id(release_name, dataset)}"
        f":{model_id}:{gene_token}:{measurement_type.value}"
    )


# ---------------------------------------------------------------------------
# integrity hashing
# ---------------------------------------------------------------------------


def _hasher(algorithm: str):
    name = algorithm.replace("-", "").lower()
    if name == "sha256":
        return hashlib.sha256()
    if name == "md5":
        # Only used to reproduce a checksum the source published. It is not a
        # security primitive here.
        return hashlib.md5(usedforsecurity=False)
    raise ValueError(f"unsupported integrity hash algorithm {algorithm!r}")


def file_integrity_hash(path: Path, *, algorithm: str = LOCAL_INTEGRITY_ALGORITHM) -> str:
    """Stream a file through one hash function and return the hex digest."""
    digest = _hasher(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_integrity_hash(text: str, *, algorithm: str = LOCAL_INTEGRITY_ALGORITHM) -> str:
    """Hash already-decoded text, encoded as UTF-8 bytes."""
    digest = _hasher(algorithm)
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DepMapManifestRow:
    """One row of the official downloadable-file manifest."""

    release: str
    filename: str
    release_date: str | None = None
    url: str | None = None
    md5_hash: str | None = None

    @property
    def durable_url(self) -> str | None:
        """The URL only when it is durable.

        DepMap's URL-bearing manifest has historically returned temporary signed
        URLs. A URL carrying a query string is a transport detail that expires,
        so it is never recorded as the source identity; ``None`` tells the
        caller to rely on ``official_file_identity``.
        """
        if not self.url:
            return None
        return None if urlparse(self.url).query else self.url


@dataclass(frozen=True)
class DepMapReleaseSelection:
    """The manifest rows selected for one explicitly requested release."""

    release_name: str
    release_date: str | None
    files: Mapping[DepMapDataset, DepMapManifestRow]


def parse_manifest(text: str, *, source: str) -> tuple[DepMapManifestRow, ...]:
    """Parse known manifest fields while tolerating future extra columns.

    Required columns remain strict. Unknown columns are ignored rather than
    assigned any scientific or operational meaning automatically.
    """
    rows = list(csv.reader(io.StringIO(_strip_bom(text))))
    if not rows:
        raise DepMapCollectionError(
            DepMapFailureReason.MANIFEST_MALFORMED, f"{source} is empty"
        )
    header = [cell.strip() for cell in rows[0]]
    if len(set(header)) != len(header):
        raise DepMapCollectionError(
            DepMapFailureReason.MANIFEST_MALFORMED,
            f"{source} declares a duplicated column: {header}",
        )
    missing = [name for name in MANIFEST_REQUIRED_COLUMNS if name not in header]
    if missing:
        raise DepMapCollectionError(
            DepMapFailureReason.MANIFEST_MALFORMED,
            f"{source} is missing required column(s) {missing}; found {header}",
        )
    positions = {name: header.index(name) for name in MANIFEST_COLUMNS if name in header}
    parsed: list[DepMapManifestRow] = []
    for line_number, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        if len(row) != len(header):
            raise DepMapCollectionError(
                DepMapFailureReason.MANIFEST_MALFORMED,
                f"{source} line {line_number} has {len(row)} fields but the header"
                f" declares {len(header)}",
            )

        def cell(name: str) -> str | None:
            position = positions.get(name)
            if position is None:
                return None
            value = row[position].strip()
            return value or None

        release = cell("release")
        filename = cell("filename")
        if release is None or filename is None:
            raise DepMapCollectionError(
                DepMapFailureReason.MANIFEST_MALFORMED,
                f"{source} line {line_number} has no release or no filename",
            )
        parsed.append(
            DepMapManifestRow(
                release=release,
                filename=filename,
                release_date=cell("release_date"),
                url=cell("url"),
                md5_hash=cell("md5_hash"),
            )
        )
    if not parsed:
        raise DepMapCollectionError(
            DepMapFailureReason.MANIFEST_MALFORMED,
            f"{source} lists no files",
        )
    return tuple(parsed)


def select_release(
    rows: Sequence[DepMapManifestRow], *, release_name: str
) -> DepMapReleaseSelection:
    """Select exactly one manifest row per required file for one release.

    Selection is an exact match on the full release name after whitespace
    normalization — never a substring, prefix or quarter-only match — because a
    record from the wrong release must be impossible to collect rather than
    merely unlikely.
    """
    wanted = normalize_release_name(release_name)
    matched = [row for row in rows if normalize_release_name(row.release) == wanted]
    if not matched:
        available = sorted({normalize_release_name(row.release) for row in rows})
        raise DepMapCollectionError(
            DepMapFailureReason.RELEASE_NOT_FOUND,
            f"release {wanted!r} is not listed in the DepMap manifest; available"
            f" releases: {available}",
        )

    dates = sorted({row.release_date for row in matched if row.release_date})
    if len(dates) > 1:
        raise DepMapCollectionError(
            DepMapFailureReason.MANIFEST_MALFORMED,
            f"release {wanted!r} is listed with conflicting release dates {dates}",
        )

    files: dict[DepMapDataset, DepMapManifestRow] = {}
    for dataset in REQUIRED_DATASETS:
        candidates = [row for row in matched if row.filename == dataset.filename]
        if not candidates:
            raise DepMapCollectionError(
                DepMapFailureReason.MANIFEST_MALFORMED,
                f"release {wanted!r} has no manifest row for required file"
                f" {dataset.filename!r}",
            )
        if len(candidates) > 1:
            raise DepMapCollectionError(
                DepMapFailureReason.MANIFEST_MALFORMED,
                f"release {wanted!r} lists {len(candidates)} manifest rows for"
                f" required file {dataset.filename!r}; exactly one is required",
            )
        files[dataset] = candidates[0]

    return DepMapReleaseSelection(
        release_name=wanted, release_date=dates[0] if dates else None, files=files
    )


@runtime_checkable
class DepMapManifestProvider(Protocol):
    """Where the official manifest text comes from."""

    @property
    def source_url(self) -> str | None:
        """The manifest endpoint, when the provider has one."""
        ...

    def read(self) -> str:
        """Return the manifest text, or raise :class:`DepMapCollectionError`."""
        ...


class LocalDepMapManifestProvider:
    """Read a manifest the caller saved from the official endpoint."""

    def __init__(self, path: str | Path, *, source_url: str | None = None) -> None:
        self._path = Path(path)
        self._source_url = source_url

    @property
    def path(self) -> Path:
        return self._path

    @property
    def source_url(self) -> str | None:
        return self._source_url

    def read(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise DepMapCollectionError(
                DepMapFailureReason.MANIFEST_UNAVAILABLE,
                f"the local DepMap manifest {str(self._path)!r} could not be read:"
                f" {exc}",
                kind=ConnectorFailureKind.TRANSPORT,
            ) from exc


# ---------------------------------------------------------------------------
# release-file resolution
# ---------------------------------------------------------------------------


@runtime_checkable
class DepMapFileResolver(Protocol):
    """Where one selected release file is read from."""

    def resolve(self, *, release_name: str, dataset: DepMapDataset) -> Path:
        """Return the local path of one required file."""
        ...

    def expected_local_integrity_hash(
        self, *, release_name: str, dataset: DepMapDataset
    ) -> str | None:
        """Return a caller-pinned SHA-256 for the local file, when available."""
        ...


class LocalDepMapReleaseResolver:
    """Resolve files inside a caller-provided official release directory.

    Two layouts are accepted, in a fixed order: a per-release subdirectory
    (``<root>/<release name>/<filename>``) and a flat directory
    (``<root>/<filename>``). The root is always supplied by the caller, so no
    machine-specific path is baked into the code, and a file belonging to a
    different release is caught later by checksum validation rather than
    guessed at here.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        expected_local_integrity_hashes: Mapping[DepMapDataset, str] | None = None,
    ) -> None:
        self._root = Path(root)
        expected = expected_local_integrity_hashes or {}
        normalized: dict[DepMapDataset, str] = {}
        for dataset, value in expected.items():
            digest = value.strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError(
                    f"expected local SHA-256 for {dataset.value!r} must be 64"
                    " hexadecimal characters"
                )
            normalized[dataset] = digest
        self._expected_local_integrity_hashes = normalized

    @property
    def root(self) -> Path:
        return self._root

    def candidate_paths(
        self, *, release_name: str, dataset: DepMapDataset
    ) -> tuple[Path, ...]:
        normalized = normalize_release_name(release_name)
        return (
            self._root / normalized / dataset.filename,
            self._root / dataset.filename,
        )

    def resolve(self, *, release_name: str, dataset: DepMapDataset) -> Path:
        candidates = self.candidate_paths(release_name=release_name, dataset=dataset)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        tried = ", ".join(str(path) for path in candidates)
        raise DepMapCollectionError(
            DepMapFailureReason.MISSING_RELEASE_FILE,
            f"required DepMap file {dataset.filename!r} for release"
            f" {normalize_release_name(release_name)!r} was not found; looked in:"
            f" {tried}",
        )

    def expected_local_integrity_hash(
        self, *, release_name: str, dataset: DepMapDataset
    ) -> str | None:
        """Return the import boundary's expected hash, if the caller supplied it."""
        return self._expected_local_integrity_hashes.get(dataset)


# ---------------------------------------------------------------------------
# gene identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneColumnIdentity:
    """A gene's source identity, exactly as the release spells it."""

    raw_header: str
    symbol: str | None
    entrez_gene_id: str | None

    def __post_init__(self) -> None:
        if self.symbol is None and self.entrez_gene_id is None:
            raise ValueError(
                f"gene identity {self.raw_header!r} carries neither a symbol nor"
                " an identifier"
            )

    @property
    def identity_key(self) -> str:
        """Case-normalized key; the source spelling is never replaced by it."""
        return f"{(self.symbol or '').casefold()}|{self.entrez_gene_id or ''}"

    def describe(self) -> str:
        return (
            f"{self.symbol or '<no symbol>'}"
            f" (Entrez {self.entrez_gene_id or '<none>'})"
        )


def parse_matrix_gene_header(raw: str) -> GeneColumnIdentity | None:
    """Parse the documented ``SYMBOL (ENTREZ)`` matrix header form.

    Fully anchored: a header that does not match exactly is refused rather than
    partially interpreted, because a half-parsed identity is how a wrong gene
    gets collected.
    """
    match = _MATRIX_GENE_HEADER.match(raw.strip())
    if match is None:
        return None
    return GeneColumnIdentity(
        raw_header=raw.strip(),
        symbol=match.group("symbol"),
        entrez_gene_id=match.group("entrez"),
    )


def parse_gene_set_token(raw: str) -> GeneColumnIdentity | None:
    """Parse one gene-set cell into a source identity.

    Three forms are accepted: the combined ``SYMBOL (ENTREZ)`` form, a bare
    symbol, and a bare all-digit token, which can only be an Entrez ID because a
    HUGO symbol always contains a letter. Nothing is inferred beyond that — in
    particular no symbol is ever matched to a different symbol by similarity.
    """
    token = raw.strip()
    if not token:
        return None
    combined = parse_matrix_gene_header(token)
    if combined is not None:
        return combined
    if token.isdigit():
        return GeneColumnIdentity(
            raw_header=token, symbol=None, entrez_gene_id=token
        )
    return GeneColumnIdentity(raw_header=token, symbol=token, entrez_gene_id=None)


@dataclass(frozen=True)
class ParsedModel:
    """One ``Model.csv`` row, restricted to the fields v0.1 normalizes."""

    model_id: str
    cell_line_name: str | None = None
    depmap_model_type: str | None = None
    oncotree_lineage: str | None = None
    oncotree_primary_disease: str | None = None
    oncotree_subtype: str | None = None

    def context_values(self) -> tuple[str, ...]:
        """The source fields a disease query may be compared against."""
        return tuple(
            value
            for value in (
                self.depmap_model_type,
                self.oncotree_lineage,
                self.oncotree_primary_disease,
                self.oncotree_subtype,
            )
            if value
        )


@dataclass(frozen=True)
class GeneMetadataIndex:
    """``Gene.csv`` as a validated lookup, never as an alias resolver."""

    entries: tuple[GeneColumnIdentity, ...]
    by_entrez: Mapping[str, GeneColumnIdentity]
    by_symbol: Mapping[str, GeneColumnIdentity]

    def __len__(self) -> int:
        return len(self.entries)

    def validate(self, identity: GeneColumnIdentity) -> GeneColumnIdentity | None:
        """Find the release's own record for ``identity``, or ``None``.

        A symbol/identifier pair that contradicts the release's own gene
        metadata is not a lookup miss to be papered over; it is reported as a
        source inconsistency by the caller.
        """
        entry: GeneColumnIdentity | None = None
        if identity.entrez_gene_id is not None:
            entry = self.by_entrez.get(identity.entrez_gene_id)
        if entry is None and identity.symbol is not None:
            entry = self.by_symbol.get(identity.symbol.casefold())
        return entry


@dataclass(frozen=True)
class ParsedGeneSet:
    """One source-defined essentiality list, fully parsed."""

    members: tuple[GeneColumnIdentity, ...]

    def __len__(self) -> int:
        return len(self.members)

    def contains(self, identity: GeneColumnIdentity) -> bool:
        """Exact identity membership under the source's own spelling.

        A shared identifier decides when both sides carry one. Otherwise the
        symbols must match exactly after case normalization. Similarity,
        prefixes, aliases and ontology links are never used.
        """
        for member in self.members:
            if (
                member.entrez_gene_id is not None
                and identity.entrez_gene_id is not None
            ):
                if member.entrez_gene_id == identity.entrez_gene_id:
                    return True
                continue
            if (
                member.symbol is not None
                and identity.symbol is not None
                and member.symbol.casefold() == identity.symbol.casefold()
            ):
                return True
        return False


# ---------------------------------------------------------------------------
# CSV readers
# ---------------------------------------------------------------------------


def _read_csv_rows(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(_strip_bom(text)))]


def _strip_bom(text: str) -> str:
    """Drop a leading byte-order mark.

    A BOM is an encoding artefact, not part of a filename or column name. Real
    DepMap CSV exports commonly carry one, and a BOM left on the first header
    cell would otherwise make every file look like it had no ``ModelID`` column
    and no recognizable gene metadata.
    """
    return text[1:] if text.startswith("\ufeff") else text


def parse_model_metadata(text: str, *, source: str) -> tuple[ParsedModel, ...]:
    """Parse ``Model.csv``, keeping only the fields v0.1 normalizes.

    ``ModelID`` is structurally required. Every other column is optional: a
    release that does not carry one leaves the corresponding field ``None``
    instead of having a value invented for it.
    """
    rows = _read_csv_rows(text)
    if not rows:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE, f"{source} is empty"
        )
    header = [cell.strip() for cell in rows[0]]
    positions = {name.casefold(): index for index, name in enumerate(header)}
    if MODEL_ID_FIELD not in positions:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE,
            f"{source} has no {MODEL_ID_COLUMN!r} column; header is {header}",
        )
    model_id_index = positions[MODEL_ID_FIELD]
    optional = {
        positions[field]: attribute
        for field, attribute in MODEL_OPTIONAL_FIELDS.items()
        if field in positions
    }

    models: list[ParsedModel] = []
    seen: set[str] = set()
    for line_number, row in enumerate(rows[1:], start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{source} line {line_number} has {len(row)} fields but the header"
                f" declares {len(header)}",
            )
        model_id = row[model_id_index].strip()
        if not model_id:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{source} line {line_number} has an empty {MODEL_ID_COLUMN}",
            )
        if model_id in seen:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{source} declares {MODEL_ID_COLUMN} {model_id!r} more than once",
            )
        seen.add(model_id)
        values = {
            # Preserve the source vocabulary exactly when the cell is not
            # blank. Technical normalization happens only in the comparison
            # key and never replaces this raw value.
            attribute: (row[index] if row[index].strip() else None)
            for index, attribute in optional.items()
        }
        models.append(ParsedModel(model_id=model_id, **values))

    if not models:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE, f"{source} lists no models"
        )
    return tuple(models)


def parse_gene_metadata(text: str, *, source: str) -> GeneMetadataIndex:
    """Parse ``Gene.csv`` into a validated symbol/identifier index.

    The file is release-matched gene metadata, used to check that a matrix
    column's identity is one the release itself declares. A header this parser
    cannot recognize is refused loudly: guessing which column holds a gene would
    make identity validation meaningless.
    """
    rows = [
        row
        for row in _read_csv_rows(text)
        if row and any(cell.strip() for cell in row)
    ]
    if not rows:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE, f"{source} is empty"
        )
    header = [cell.strip() for cell in rows[0]]
    if len(set(cell.casefold() for cell in header)) != len(header):
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE,
            f"{source} declares a duplicated column: {header}",
        )

    body = rows[1:]
    entries: list[GeneColumnIdentity] = []

    combined = _single_combined_identity_column(header, body, source=source)
    if combined is not None:
        entries = combined
    else:
        fold = [cell.casefold() for cell in header]
        symbol_index = _first_index(fold, _GENE_SYMBOL_FIELD_NAMES)
        entrez_index = _first_index(fold, _GENE_ENTREZ_FIELD_NAMES)
        if symbol_index is None and entrez_index is None and len(header) == 1:
            # A single unnamed gene column: all-digit values can only be
            # identifiers, anything else is read as a symbol.
            values = [row[0].strip() for row in body if row and row[0].strip()]
            if values and all(value.isdigit() for value in values):
                entrez_index = 0
            else:
                symbol_index = 0
        if symbol_index is None and entrez_index is None:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{source} has no recognisable gene symbol or identifier column;"
                f" header is {header}",
            )
        for line_number, row in enumerate(body, start=2):
            if len(row) != len(header):
                raise DepMapCollectionError(
                    DepMapFailureReason.MALFORMED_DATASET_FILE,
                    f"{source} line {line_number} has {len(row)} fields but the"
                    f" header declares {len(header)}",
                )
            symbol = row[symbol_index].strip() if symbol_index is not None else ""
            entrez = row[entrez_index].strip() if entrez_index is not None else ""
            if not symbol and not entrez:
                raise DepMapCollectionError(
                    DepMapFailureReason.MALFORMED_DATASET_FILE,
                    f"{source} line {line_number} carries no gene identity",
                )
            if entrez and not entrez.isdigit():
                raise DepMapCollectionError(
                    DepMapFailureReason.MALFORMED_DATASET_FILE,
                    f"{source} line {line_number} has a non-numeric gene"
                    f" identifier {entrez!r}",
                )
            entries.append(
                GeneColumnIdentity(
                    raw_header=(f"{symbol} ({entrez})" if symbol else entrez),
                    symbol=symbol or None,
                    entrez_gene_id=entrez or None,
                )
            )

    return _index_gene_entries(entries, source=source)


def _single_combined_identity_column(
    header: Sequence[str], body: Sequence[Sequence[str]], *, source: str
) -> list[GeneColumnIdentity] | None:
    """Read a one-column file whose cells are ``SYMBOL (ENTREZ)``, if it is one."""
    if len(header) != 1:
        return None
    values = [row[0].strip() for row in body if row and row[0].strip()]
    if not values:
        return None
    parsed = [parse_matrix_gene_header(value) for value in values]
    if any(identity is None for identity in parsed):
        return None
    return [identity for identity in parsed if identity is not None]


def _first_index(folded: Sequence[str], names: frozenset[str]) -> int | None:
    for index, name in enumerate(folded):
        if name in names:
            return index
    return None


def _index_gene_entries(
    entries: Iterable[GeneColumnIdentity], *, source: str
) -> GeneMetadataIndex:
    """Build both lookups, refusing two records that contradict each other."""
    ordered: list[GeneColumnIdentity] = []
    by_entrez: dict[str, GeneColumnIdentity] = {}
    by_symbol: dict[str, GeneColumnIdentity] = {}
    seen_keys: set[str] = set()
    for entry in entries:
        if entry.identity_key in seen_keys:
            # An exact repeat carries no extra information in a lookup table.
            continue
        if entry.entrez_gene_id is not None:
            previous = by_entrez.get(entry.entrez_gene_id)
            if previous is not None:
                raise DepMapCollectionError(
                    DepMapFailureReason.MALFORMED_DATASET_FILE,
                    f"{source} maps Entrez id {entry.entrez_gene_id} to both"
                    f" {previous.describe()} and {entry.describe()}",
                )
            by_entrez[entry.entrez_gene_id] = entry
        if entry.symbol is not None:
            key = entry.symbol.casefold()
            previous = by_symbol.get(key)
            if previous is not None:
                raise DepMapCollectionError(
                    DepMapFailureReason.MALFORMED_DATASET_FILE,
                    f"{source} maps symbol {entry.symbol} to both"
                    f" {previous.describe()} and {entry.describe()}",
                )
            by_symbol[key] = entry
        seen_keys.add(entry.identity_key)
        ordered.append(entry)
    if not ordered:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE, f"{source} lists no genes"
        )
    return GeneMetadataIndex(
        entries=tuple(ordered), by_entrez=by_entrez, by_symbol=by_symbol
    )


def parse_gene_set(text: str, *, source: str) -> ParsedGeneSet:
    """Parse a single-column essentiality list, completely.

    Membership may only be written after the whole list parsed successfully, so
    a truncated or malformed list fails the run instead of being read as
    ``NON_MEMBER``.
    """
    rows = _read_csv_rows(text)
    if not rows:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE, f"{source} is empty"
        )

    start = 0
    first = rows[0]
    if len(first) == 1 and first[0].strip().casefold() in _GENE_SET_HEADER_NAMES:
        start = 1

    members: list[GeneColumnIdentity] = []
    seen: set[str] = set()
    for line_number, row in enumerate(rows[start:], start=start + 1):
        if not row:
            continue
        if len(row) != 1:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{source} line {line_number} has {len(row)} fields; a gene set is"
                " a single column",
            )
        if not row[0].strip():
            continue
        identity = parse_gene_set_token(row[0])
        if identity is None:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{source} line {line_number} has no gene identity",
            )
        if identity.identity_key in seen:
            continue
        seen.add(identity.identity_key)
        members.append(identity)

    if not members:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE,
            f"{source} lists no genes; membership cannot be established from an"
            " empty list",
        )
    return ParsedGeneSet(members=tuple(members))


def _parse_matrix_header(
    header: Sequence[str], *, source: str
) -> tuple[tuple[str, ...], tuple[GeneColumnIdentity | None, ...]]:
    """Validate a CRISPR matrix header row and parse its gene columns.

    An empty header cell carries no identity and is skipped; every other cell
    must be the documented ``SYMBOL (ENTREZ)`` form, and no two columns may
    declare the same header.
    """
    if not header or header[0].strip() != MODEL_ID_COLUMN:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE,
            f"{source} must start with a {MODEL_ID_COLUMN!r} column; header"
            f" starts with {header[0] if header else '<nothing>'!r}",
        )
    raw_headers = tuple(cell.strip() for cell in header[1:])
    identities: list[GeneColumnIdentity | None] = []
    seen_raw: set[str] = set()
    for position, raw in enumerate(raw_headers):
        if not raw:
            identities.append(None)
            continue
        identity = parse_matrix_gene_header(raw)
        if identity is None:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{source} column {position + 2} header {raw!r} is not the"
                " documented 'SYMBOL (ENTREZ)' form",
            )
        if raw in seen_raw:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{source} declares gene column {raw!r} more than once",
            )
        seen_raw.add(raw)
        identities.append(identity)
    return raw_headers, tuple(identities)


@dataclass(frozen=True)
class MatrixHeaders:
    """The parsed gene-column header of one CRISPR matrix, and nothing else."""

    raw_headers: tuple[str, ...]
    identities: tuple[GeneColumnIdentity | None, ...]
    source: str

    def present_identities(self) -> tuple[GeneColumnIdentity, ...]:
        return tuple(
            identity for identity in self.identities if identity is not None
        )


def read_matrix_headers(path: Path, *, dataset: DepMapDataset) -> MatrixHeaders:
    """Read only a CRISPR matrix's header row.

    Resolution needs to know which gene columns a release has before it knows
    which column to stream, so the header is read on its own. The full matrix is
    still read and validated afterwards.
    """
    source = f"{dataset.filename} ({dataset.value})"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE, f"{source} is empty"
            ) from None
    raw_headers, identities = _parse_matrix_header(header, source=source)
    return MatrixHeaders(
        raw_headers=raw_headers, identities=identities, source=source
    )


def resolve_gene_identity(
    headers: Sequence[MatrixHeaders], gene_input: str
) -> tuple[GeneColumnIdentity | None, GeneResolutionBasis, tuple[GeneColumnIdentity, ...]]:
    """Resolve a caller's gene text against the release's own gene columns.

    An all-digit input is an explicit Entrez identifier; anything else is an
    exact symbol, compared case-insensitively so human input is usable while the
    source spelling is preserved. No alias, retired symbol or synonym is ever
    substituted.

    Returns ``(identity, basis, candidates)``. ``identity`` is ``None`` when the
    release simply has no such column, which is a legitimate absence.
    Multiple distinct matches are refused outright: reporting one of them would
    risk collecting the wrong gene.
    """
    token = gene_input.strip()
    basis = (
        GeneResolutionBasis.EXPLICIT_ENTREZ
        if token.isdigit()
        else GeneResolutionBasis.EXACT_SYMBOL
    )

    candidates: list[GeneColumnIdentity] = []
    seen: set[str] = set()
    for header in headers:
        for identity in header.present_identities():
            if identity.identity_key in seen:
                continue
            if basis is GeneResolutionBasis.EXPLICIT_ENTREZ:
                matched = identity.entrez_gene_id == token
            else:
                matched = (
                    identity.symbol is not None
                    and identity.symbol.casefold() == token.casefold()
                )
            if matched:
                seen.add(identity.identity_key)
                candidates.append(identity)

    if not candidates:
        return None, basis, ()
    if len(candidates) > 1:
        described = ", ".join(identity.raw_header for identity in candidates)
        raise DepMapCollectionError(
            DepMapFailureReason.GENE_IDENTITY_AMBIGUOUS,
            f"gene input {gene_input!r} matches more than one DepMap source"
            f" identity ({described}); the query is refused rather than resolved"
            " to a guess",
        )
    return candidates[0], basis, tuple(candidates)


@dataclass(frozen=True)
class MatrixRead:
    """One CRISPR matrix, read once: header identities plus one gene column.

    ``raw_headers`` and ``identities`` are aligned with the matrix's own column
    order (``identities[i]`` is ``None`` for an empty header cell, which carries
    no identity). ``column_index`` is the position of the requested gene inside
    ``raw_headers``, or ``None`` when that gene has no column in this matrix.
    """

    raw_headers: tuple[str, ...]
    identities: tuple[GeneColumnIdentity | None, ...]
    column_index: int | None
    values: Mapping[str, str]
    model_order: tuple[str, ...]
    source: str

    def present_identities(self) -> tuple[GeneColumnIdentity, ...]:
        return tuple(
            identity for identity in self.identities if identity is not None
        )

    def column_for(self, identity: GeneColumnIdentity) -> int | None:
        """The CSV position of one gene identity, or ``None`` when absent."""
        return column_index_of(self.identities, identity, source=self.source)


def column_index_of(
    identities: Sequence[GeneColumnIdentity | None],
    identity: GeneColumnIdentity,
    *,
    source: str,
) -> int | None:
    """Locate one gene identity among a matrix's parsed columns."""
    matches = [
        index
        for index, candidate in enumerate(identities)
        if candidate is not None and candidate.identity_key == identity.identity_key
    ]
    if len(matches) > 1:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE,
            f"{source} gives gene identity {identity.describe()} several columns"
            f" {matches}; the matrix is internally inconsistent",
        )
    return matches[0] if matches else None


def read_matrix(
    path: Path,
    *,
    dataset: DepMapDataset,
    wanted: GeneColumnIdentity | None = None,
) -> MatrixRead:
    """Read one CRISPR matrix, streaming its rows.

    Every row is validated — the header form of every gene column, duplicate
    gene columns, duplicate ``ModelID`` rows, and row widths — even when the
    requested gene is absent, so a malformed file can never be mistaken for an
    empty answer. Only the requested gene column's values are retained; the rest
    of the matrix is never materialized.
    """
    source = f"{dataset.filename} ({dataset.value})"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE, f"{source} is empty"
            ) from None
        raw_headers, identities = _parse_matrix_header(header, source=source)

        column_index: int | None = None
        if wanted is not None:
            column_index = column_index_of(identities, wanted, source=source)

        values: dict[str, str] = {}
        model_order: list[str] = []
        expected = len(header)
        for line_number, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) != expected:
                raise DepMapCollectionError(
                    DepMapFailureReason.MALFORMED_DATASET_FILE,
                    f"{source} line {line_number} has {len(row)} fields but the"
                    f" header declares {expected}",
                )
            model_id = row[0].strip()
            if not model_id:
                raise DepMapCollectionError(
                    DepMapFailureReason.MALFORMED_DATASET_FILE,
                    f"{source} line {line_number} has an empty {MODEL_ID_COLUMN}",
                )
            if model_id in values:
                raise DepMapCollectionError(
                    DepMapFailureReason.MALFORMED_DATASET_FILE,
                    f"{source} declares {MODEL_ID_COLUMN} {model_id!r} more than"
                    " once",
                )
            values[model_id] = (
                # +1: the first CSV cell is the ModelID, and ``column_index`` is
                # an index into the gene headers that follow it.
                row[column_index + 1]
                if column_index is not None
                else ""
            )
            model_order.append(model_id)

    return MatrixRead(
        raw_headers=raw_headers,
        identities=tuple(identities),
        column_index=column_index,
        values=values,
        model_order=tuple(model_order),
        source=source,
    )


def parse_measurement_token(token: str) -> tuple[float | None, str]:
    """Turn one matrix cell into ``(value, raw_token)``.

    A missing token stays missing: it is never converted to zero, never
    interpolated, and never replaced by the other measurement type's value. A
    token that is present but not a finite number is a malformed source value,
    which is a failure rather than a number to reinterpret.
    """
    raw = token.strip()
    if raw.casefold() in MISSING_CELL_TOKENS:
        return None, raw
    try:
        value = float(raw)
    except ValueError as exc:
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE,
            f"measurement token {raw!r} is not a number",
        ) from exc
    if not math.isfinite(value):
        raise DepMapCollectionError(
            DepMapFailureReason.MALFORMED_DATASET_FILE,
            f"measurement token {raw!r} is not a finite number",
        )
    return value, raw
