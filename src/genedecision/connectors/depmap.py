"""DepMap connector: release-pinned CRISPR dependency collection.

What this connector is
----------------------
Part of the **Collection Layer**. It reads one explicitly pinned DepMap release
from caller-provided local official files, validates the release and file
identities against the official manifest, and returns factual measurements with
provenance: gene effect, dependency probability, model identity, source-defined
essentiality membership, and what a query's context did and did not cover.

What it deliberately is not
---------------------------
It does not decide anything scientific. In particular it never decides:

* whether a gene is a baseline, common, pan- or phenotype-specific dependency;
* whether a score supports or contradicts the caller's hypothesis;
* whether a candidate should be prioritized or deprioritized;
* whether one lineage differs meaningfully from another;
* whether a threshold makes a model biologically "dependent"; or
* what mechanism explains a measurement.

Every PUBLIC record it creates therefore keeps ``support = UNKNOWN`` and
``directness = UNASSESSED``. ``evidence_level`` is ``PERTURBATIONAL`` because
the selected source itself describes genome-wide CRISPR knockout screens — that
is normalization of the stated assay type, not a judgement about evidentiary
strength.

Acquisition model
-----------------
The release name is always explicit; there is no ``latest`` mode, and passing
one is refused at construction time. Release **files** are read from a local
official release directory through a resolver; no unattended remote release-file
download, portal scraping, browser automation or CAPTCHA workaround exists
anywhere in this module. The official manifest is also caller-provided as a
local file in v0.1. There is no DepMap network path in this connector.

Granularity
-----------
One profile-level ``EvidenceRecord`` per gene × release, with measurement-level
child rows beneath it. Gene effect and dependency probability are always two
separate observations, even for the same gene and model, because they come from
two datasets with two file identities. One may exist while the other is missing,
and a missing measurement is never zero.
"""

from __future__ import annotations

import hmac
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from genedecision.connectors.base import (
    ConnectorFailure,
    ConnectorQuery,
    ConnectorQueryResult,
    ConnectorRunResult,
    ConnectorStatus,
    EvidenceConnector,
)
from genedecision.connectors.depmap_release import (
    FAILURE_KINDS,
    GENE_SET_DATASETS,
    LOCAL_INTEGRITY_ALGORITHM,
    MATRIX_DATASETS,
    REQUIRED_DATASETS,
    SOURCE_DEFINITIONS,
    DepMapCollectionError,
    DepMapDataset,
    DepMapFileResolver,
    DepMapManifestProvider,
    DepMapManifestRow,
    GeneColumnIdentity,
    GeneMetadataIndex,
    GeneResolutionBasis,
    MatrixHeaders,
    MatrixRead,
    ParsedGeneSet,
    ParsedModel,
    derive_release_code,
    depmap_dataset_file_id,
    depmap_evidence_id,
    depmap_official_file_identity,
    depmap_observation_id,
    depmap_release_id,
    file_integrity_hash,
    normalize_release_name,
    parse_gene_metadata,
    parse_gene_set,
    parse_manifest,
    parse_measurement_token,
    parse_model_metadata,
    read_matrix,
    read_matrix_headers,
    resolve_gene_identity,
    select_release,
    text_integrity_hash,
)
from genedecision.models.depmap import (
    CommonEssentialAnnotation,
    CommonEssentialAnnotationType,
    CommonEssentialMembershipStatus,
    DEPMAP_SOURCE_NAME,
    DEPMAP_SOURCE_TYPE,
    DEPMAP_SOURCE_URL,
    DEPMAP_TERMS_URL,
    DataLicenseMetadata,
    DepMapCoverageState,
    DepMapDatasetFile,
    DepMapDependencyObservation,
    DepMapFailureReason,
    DepMapGeneIdentity,
    DepMapMeasurementStatus,
    DepMapMeasurementType,
    DepMapModelMetadata,
    DepMapNoDataReason,
    DepMapReleaseMetadata,
)
from genedecision.models.evidence import (
    Directness,
    EvidenceLevel,
    EvidenceRecord,
    SourceScope,
    Support,
)

#: Recorded on every DepMap observation whose ``ModelID`` is absent from the
#: release's ``Model.csv``. The observation is preserved, not dropped.
FLAG_MODEL_METADATA_UNAVAILABLE = "MODEL_METADATA_UNAVAILABLE"

#: Recorded on a profile-level record so a reader is never left guessing why the
#: aggregate's ``effect_value`` is ``None``: the measurements are child rows.
FLAG_DEPMAP_MEASUREMENTS_IN_CHILD_ROWS = (
    "DEPMAP_MEASUREMENTS_STORED_IN_CHILD_ROWS"
)

#: The context-matching policy recorded in every ``query_string``. v0.1 compares
#: query text to explicit ``Model.csv`` vocabulary only — no fuzzy matching, no
#: ontology inference, no abbreviation expansion, no synonym mapping.
DEFAULT_CONTEXT_MATCH_POLICY = "exact-source-vocabulary-v1"

DEFAULT_USAGE_NOTE = (
    "DepMap release files were read from a caller-provided local directory. The"
    " release files are published by the DepMap project at the Broad Institute;"
    " review the then-current official terms before redistribution, reuse in a"
    " product, or publication."
)
DEFAULT_LICENSE_NOTE = (
    "Recorded for provenance only. No legal determination is made here:"
    " connector support for DepMap does not imply commercial redistribution"
    " rights."
)

#: Characters that would let a caller-supplied axis restructure the recorded
#: collection request, so they are refused rather than escaped.
_RESERVED_QUERY_CHARACTERS = ('"', ";")


def normalize_vocabulary(value: str) -> str:
    """Unicode-normalize, collapse whitespace and case-fold a vocabulary value.

    This is the *only* transformation applied before an exact comparison. It
    does not expand abbreviations, map synonyms, or reach across an ontology.
    """
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


class DepMapContextCoverage(BaseModel):
    """Factual coverage of one DepMap query, as a runtime structure.

    This says what the selected release's metadata and measurements did and did
    not cover for one query. It draws no conclusion: a
    ``NO_EXACT_SOURCE_VOCABULARY_MATCH`` result means only that the query text
    did not exactly match the release's own vocabulary, never that no relevant
    model exists.

    It is derived from ``QueryMatch``, model metadata and measurement rows, and
    therefore has no dedicated persistence table in v0.1.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: str | None = None
    evidence_id: str | None = None
    query_text: str = Field(min_length=1)
    matching_policy: str = Field(min_length=1)
    matched_model_count: int = Field(ge=0)
    matched_models_with_gene_effect_count: int = Field(ge=0)
    matched_models_with_dependency_probability_count: int = Field(ge=0)
    coverage_state: DepMapCoverageState

    def with_run(self, run_id: str) -> "DepMapContextCoverage":
        """The same coverage, stamped with the run that produced it."""
        return self.model_copy(update={"run_id": run_id})


class DepMapDependencyMetadata(BaseModel):
    """The in-memory aggregate behind one gene × release DepMap profile.

    A runtime composition of already-typed factual components; it exists so the
    evidence record does not have to grow DepMap-only columns. It has no
    persistence table of its own, because every field here is recoverable from
    the normalized source records.

    ``context_coverage`` is per-query, so it is ``None`` on a profile that has
    been merged across several queries: one aggregate cannot own several
    queries' coverage. Per-query coverage stays on each
    :class:`DepMapQueryResult`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1)
    release: DepMapReleaseMetadata
    dataset_files: list[DepMapDatasetFile] = Field(default_factory=list)
    license_metadata: DataLicenseMetadata
    gene_identity: DepMapGeneIdentity
    models: list[DepMapModelMetadata] = Field(default_factory=list)
    observations: list[DepMapDependencyObservation] = Field(default_factory=list)
    common_essential_annotations: list[CommonEssentialAnnotation] = Field(
        default_factory=list
    )
    context_coverage: DepMapContextCoverage | None = None

    @model_validator(mode="after")
    def all_facts_belong_to_one_release(self) -> "DepMapDependencyMetadata":
        """Treat cross-release composition as an internal invariant violation."""
        release_id = self.release.release_id
        if self.license_metadata.release_id != release_id:
            raise ValueError("DepMap profile licensing provenance crosses releases")
        for collection_name, items in (
            ("dataset files", self.dataset_files),
            ("models", self.models),
            ("observations", self.observations),
            ("common-essential annotations", self.common_essential_annotations),
        ):
            foreign = sorted(
                {
                    item.release_id
                    for item in items
                    if item.release_id != release_id
                }
            )
            if foreign:
                raise ValueError(
                    f"DepMap profile {collection_name} cross releases: expected"
                    f" {release_id!r}, found {foreign}"
                )
        evidence_items = [
            self.gene_identity,
            *self.observations,
            *self.common_essential_annotations,
        ]
        foreign_evidence = sorted(
            {
                item.evidence_id
                for item in evidence_items
                if item.evidence_id != self.evidence_id
            }
        )
        if foreign_evidence:
            raise ValueError(
                f"DepMap profile facts cross evidence identities: expected"
                f" {self.evidence_id!r}, found {foreign_evidence}"
            )
        return self

    def source_facts_signature(self) -> tuple[Any, ...]:
        """Everything that must be identical for two queries about one profile.

        ``input_gene`` and ``resolution_basis`` are deliberately excluded: two
        queries that spell the same gene differently still describe one
        profile, and that spelling difference is not a source-fact conflict.
        ``context_coverage`` is per-query and excluded for the same reason.
        """
        identity = self.gene_identity
        return (
            self.release.model_dump(),
            tuple(item.model_dump() for item in self.dataset_files),
            self.license_metadata.model_dump(),
            (
                identity.depmap_symbol,
                identity.entrez_gene_id,
                identity.raw_matrix_header,
                identity.gene_metadata_file_id,
            ),
            tuple(item.model_dump() for item in self.models),
            tuple(item.model_dump() for item in self.observations),
            tuple(item.model_dump() for item in self.common_essential_annotations),
        )


class DepMapQueryResult(ConnectorQueryResult):
    """One DepMap query's outcome, with the facts a generic result cannot carry.

    Every missing state stays distinguishable: a failure names the exact cause,
    a ``NO_RESULTS`` names which kind of absence it is, and a success carries the
    pinned release, the factual profile, and the query's coverage.
    """

    depmap_profile: DepMapDependencyMetadata | None = None
    context_coverage: DepMapContextCoverage | None = None
    no_data_reason: DepMapNoDataReason | None = None
    failure_reason: DepMapFailureReason | None = None

    @model_validator(mode="after")
    def depmap_status_is_explained(self) -> "DepMapQueryResult":
        if self.status is ConnectorStatus.FAILURE:
            if self.failure_reason is None:
                raise ValueError(
                    "a DepMap FAILURE must name its DepMap failure reason"
                )
        elif self.failure_reason is not None:
            raise ValueError(
                f"a {self.status.value} result must not carry a failure reason"
            )

        if self.status is ConnectorStatus.NO_RESULTS:
            if self.no_data_reason is None:
                raise ValueError(
                    "a DepMap NO_RESULTS must say which kind of absence it is"
                )
        elif self.no_data_reason is not None:
            raise ValueError(
                f"a {self.status.value} result must not carry a no-data reason"
            )

        if self.status is ConnectorStatus.SUCCESS and self.depmap_profile is None:
            raise ValueError(
                "a DepMap SUCCESS must carry the profile it collected"
            )
        if self.depmap_profile is not None:
            known = {record.evidence_id for record in self.records}
            if self.depmap_profile.evidence_id not in known:
                raise ValueError(
                    "a DepMap profile must belong to a record in this result:"
                    f" {self.depmap_profile.evidence_id!r} is not one of"
                    f" {sorted(known)}"
                )
        return self


class DepMapRunResult(ConnectorRunResult):
    """One connector run over several queries, with DepMap provenance.

    ``release``, ``dataset_files`` and ``license_metadata`` describe the exact
    release bundle the run used, and are retained even when every query was a
    clean ``NO_RESULTS``, so an empty answer stays traceable to the release it
    was empty in. They are absent only when the run never reached a validated
    release, in which case no profile may exist either.
    """

    query_results: list[DepMapQueryResult] = Field(min_length=1)
    release: DepMapReleaseMetadata | None = None
    dataset_files: list[DepMapDatasetFile] = Field(default_factory=list)
    license_metadata: DataLicenseMetadata | None = None
    depmap_profiles: dict[str, DepMapDependencyMetadata] = Field(default_factory=dict)

    @model_validator(mode="after")
    def provenance_matches_outcome(self) -> "DepMapRunResult":
        answered = any(
            result.status is not ConnectorStatus.FAILURE
            for result in self.query_results
        )
        if answered and (self.release is None or self.license_metadata is None):
            raise ValueError(
                "a DepMap run that answered any query must carry its pinned"
                " release metadata and its licensing provenance"
            )
        if self.release is None and (self.dataset_files or self.depmap_profiles):
            raise ValueError(
                "a DepMap run without a validated release cannot carry dataset"
                " files or dependency profiles"
            )
        known = {record.evidence_id for record in self.records}
        unknown = sorted(set(self.depmap_profiles) - known)
        if unknown:
            raise ValueError(
                "DepMap profiles without a record: " + ", ".join(unknown)
            )
        if self.release is not None:
            release_id = self.release.release_id
            if (
                self.license_metadata is not None
                and self.license_metadata.release_id != release_id
            ):
                raise ValueError("DepMap run licensing provenance crosses releases")
            foreign = sorted(
                {
                    item.release_id
                    for item in self.dataset_files
                    if item.release_id != release_id
                }
                | {
                    profile.release.release_id
                    for profile in self.depmap_profiles.values()
                    if profile.release.release_id != release_id
                }
            )
            if foreign:
                raise ValueError(
                    f"DepMap run cannot mix releases: expected {release_id!r},"
                    f" found {foreign}"
                )
        return self

    def profile(self, evidence_id: str) -> DepMapDependencyMetadata | None:
        """The merged profile for one evidence item, if this run collected it."""
        return self.depmap_profiles.get(evidence_id)


def merge_depmap_profiles(
    results: Sequence[DepMapQueryResult],
) -> dict[str, DepMapDependencyMetadata]:
    """Collapse per-query profiles into one profile per evidence id.

    Several context questions about the same gene and release are one evidence
    item, so they merge onto one deterministic ``evidence_id``. The source facts
    must be *identical* across those queries; if they disagree, the run fails
    rather than silently keeping whichever happened to come first.
    """
    merged: dict[str, DepMapDependencyMetadata] = {}
    signatures: dict[str, tuple[Any, ...]] = {}
    pinned_release_id: str | None = None
    for result in results:
        profile = result.depmap_profile
        if profile is None:
            continue
        release_id = profile.release.release_id
        if pinned_release_id is None:
            pinned_release_id = release_id
        elif release_id != pinned_release_id:
            raise ValueError(
                "cannot merge DepMap profiles across releases: expected"
                f" {pinned_release_id!r}, found {release_id!r}"
            )
        signature = profile.source_facts_signature()
        existing = signatures.get(profile.evidence_id)
        if existing is None:
            signatures[profile.evidence_id] = signature
            merged[profile.evidence_id] = profile.model_copy(
                update={"context_coverage": None}
            )
            continue
        if existing != signature:
            differing = [
                index
                for index, (left, right) in enumerate(zip(existing, signature))
                if left != right
            ]
            raise ValueError(
                f"conflicting DepMap source facts for evidence id"
                f" {profile.evidence_id!r} across queries (differing component(s)"
                f" {differing}); a run must not choose one silently"
            )
    return merged


@dataclass(frozen=True)
class _ReleaseBundle:
    """One validated, release-pinned file bundle, parsed once and reused."""

    release: DepMapReleaseMetadata
    dataset_files: Mapping[DepMapDataset, DepMapDatasetFile]
    license: DataLicenseMetadata
    paths: Mapping[DepMapDataset, Path]
    models: tuple[DepMapModelMetadata, ...]
    model_order: tuple[str, ...]
    model_context: Mapping[str, frozenset[str]]
    gene_index: GeneMetadataIndex
    gene_sets: Mapping[CommonEssentialAnnotationType, ParsedGeneSet]

    def ordered_files(self) -> tuple[DepMapDatasetFile, ...]:
        """Dataset files in the release's fixed order, for reproducible output."""
        return tuple(self.dataset_files[dataset] for dataset in REQUIRED_DATASETS)


@dataclass(frozen=True)
class _GeneFacts:
    """Everything the release reports about one gene identity."""

    identity: GeneColumnIdentity
    evidence_id: str
    resolution_basis: GeneResolutionBasis
    observations: tuple[DepMapDependencyObservation, ...]
    annotations: tuple[CommonEssentialAnnotation, ...]
    measured_gene_effect: frozenset[str] = field(default_factory=frozenset)
    measured_dependency: frozenset[str] = field(default_factory=frozenset)

    @property
    def measured_model_count(self) -> int:
        return len(self.measured_gene_effect | self.measured_dependency)


class DepMapConnector(EvidenceConnector):
    """Collect one gene's DepMap dependency profile from one pinned release.

    The release is fixed when the connector is built and every query against it
    is answered from that same release; a record can never be mixed from two
    releases.
    """

    name = DEPMAP_SOURCE_NAME
    source_scope = SourceScope.PUBLIC

    def __init__(
        self,
        *,
        release_name: str,
        file_resolver: DepMapFileResolver,
        manifest_provider: DepMapManifestProvider,
        terms_url: str | None = DEPMAP_TERMS_URL,
        usage_note: str | None = DEFAULT_USAGE_NOTE,
        license_note: str | None = DEFAULT_LICENSE_NOTE,
        context_match_policy: str = DEFAULT_CONTEXT_MATCH_POLICY,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Pin a release and the places its files come from.

        ``release_name`` must be explicit. ``latest`` is refused at construction
        because v0.1 has no latest mode: a scientific run must name the release
        whose numbers it will record, and a connector that could silently follow
        the newest release would make old provenance meaningless.

        ``manifest_provider`` is required and must provide a caller-saved
        official manifest. Milestone 3B has no unattended network path for
        manifest discovery or release-file acquisition.
        """
        super().__init__(clock=clock)
        normalized = normalize_release_name(release_name)
        if not normalized:
            raise ValueError("a DepMap connector requires an explicit release name")
        if normalized.casefold() == "latest":
            raise ValueError(
                "DepMap release 'latest' is not supported; v0.1 requires an"
                " explicit release such as 'DepMap Public 26Q1' so every"
                " collected value stays traceable to the release it came from"
            )
        self._release_name = normalized
        self._resolver = file_resolver
        self._manifest_provider = manifest_provider
        self._terms_url = terms_url
        self._usage_note = usage_note
        self._license_note = license_note
        self._context_match_policy = context_match_policy

        self._bundle: _ReleaseBundle | None = None
        self._matrix_headers: Mapping[DepMapDataset, MatrixHeaders] | None = None
        self._facts_cache: dict[str, _GeneFacts | None] = {}

    # -- configuration -----------------------------------------------------

    @property
    def release_name(self) -> str:
        return self._release_name

    @property
    def resolver(self) -> DepMapFileResolver:
        return self._resolver

    @property
    def manifest_provider(self) -> DepMapManifestProvider:
        return self._manifest_provider

    def cached_bundle(self) -> _ReleaseBundle | None:
        """The validated bundle, once one has been loaded."""
        return self._bundle

    # -- query rendering ---------------------------------------------------

    def build_query_string(self, query: ConnectorQuery) -> str:
        """Serialize the collection request deterministically.

        This is a recorded GeneDecision collection request — release, input
        gene, supplied context axes and the context-matching policy — not a
        claim that DepMap offers a remote search endpoint with this syntax. An
        axis containing a character that would restructure the request is
        refused rather than escaped, so what is recorded is exactly what was
        asked.
        """
        parts: list[tuple[str, str]] = [
            ("release", self._release_name),
            ("gene", query.gene.strip()),
            *query.context_axes().items(),
            ("context_match", self._context_match_policy),
        ]
        rendered: list[str] = []
        for axis, value in parts:
            text = value.strip()
            if not text:
                raise ValueError(f"query axis {axis!r} is empty")
            offending = [c for c in _RESERVED_QUERY_CHARACTERS if c in text]
            if offending:
                raise ValueError(
                    f"query axis {axis!r} contains reserved character(s) "
                    + ", ".join(repr(c) for c in offending)
                    + "; the axis is refused rather than rewritten"
                )
            rendered.append(f'{axis}="{text}"')
        return ";".join(rendered)

    # -- execution ---------------------------------------------------------

    def search(
        self,
        query: ConnectorQuery,
        *,
        retrieved_at: datetime | None = None,
    ) -> DepMapQueryResult:
        """Collect one gene's release-pinned profile; classify failures."""
        timestamp = retrieved_at if retrieved_at is not None else self.now()
        try:
            query_string = self.build_query_string(query)
        except ValueError as exc:
            return self._failure(
                query,
                reason=DepMapFailureReason.INVALID_QUERY,
                message=str(exc),
                retrieved_at=timestamp,
                query_string=None,
            )

        try:
            bundle = self._ensure_bundle(timestamp)
            facts = self._gene_facts(bundle, query.gene, timestamp)
        except DepMapCollectionError as failure:
            return self._failure(
                query,
                reason=failure.reason,
                message=failure.message,
                retrieved_at=timestamp,
                query_string=query_string,
                http_status=failure.http_status,
            )

        coverage = self._coverage(bundle, query, facts)

        if facts is None:
            return self._no_results(
                query,
                query_string=query_string,
                retrieved_at=timestamp,
                reason=DepMapNoDataReason.GENE_NOT_PRESENT_IN_SELECTED_DATASET,
                coverage=coverage,
            )
        if facts.measured_model_count == 0:
            return self._no_results(
                query,
                query_string=query_string,
                retrieved_at=timestamp,
                reason=DepMapNoDataReason.NO_APPLICABLE_MEASUREMENT_FOR_RESOLVED_GENE,
                coverage=coverage,
            )

        record = self._evidence_record(facts.identity, bundle, timestamp)
        profile = self._profile(bundle, facts, query, coverage)
        return DepMapQueryResult(
            connector=self.name,
            query=query,
            query_string=query_string,
            status=ConnectorStatus.SUCCESS,
            retrieved_at=timestamp,
            total_matches=len(facts.observations),
            records=[record],
            depmap_profile=profile,
            context_coverage=coverage,
        )

    def search_many(
        self, queries: Sequence[ConnectorQuery]
    ) -> DepMapRunResult:
        """Run several queries and merge them into one traceable DepMap run.

        The generic merge already collapses duplicate evidence ids and keeps
        every matching query. DepMap profiles are merged alongside it, by the
        same deterministic ``evidence_id``, and the pinned release bundle is
        attached so an empty answer remains traceable too.
        """
        if not queries:
            raise ValueError("a run must contain at least one query")
        base = super().search_many(queries)
        run_id = base.run_id
        results = [
            result.model_copy(
                update={
                    "context_coverage": (
                        None
                        if result.context_coverage is None
                        else result.context_coverage.with_run(run_id)
                    )
                }
            )
            for result in base.query_results
        ]
        bundle = self._bundle
        return DepMapRunResult(
            run_id=run_id,
            connector=self.name,
            source_scope=self.source_scope,
            retrieved_at=base.retrieved_at,
            query_results=results,
            records=base.records,
            publications=base.publications,
            query_matches=base.query_matches,
            release=None if bundle is None else bundle.release,
            dataset_files=[] if bundle is None else list(bundle.ordered_files()),
            license_metadata=None if bundle is None else bundle.license,
            depmap_profiles=merge_depmap_profiles(results),
        )

    # -- release loading ---------------------------------------------------

    def _ensure_bundle(self, retrieved_at: datetime) -> _ReleaseBundle:
        """Load, validate and parse the pinned release exactly once."""
        if self._bundle is None:
            self._bundle = self._load_bundle(retrieved_at)
        return self._bundle

    def _load_bundle(self, retrieved_at: datetime) -> _ReleaseBundle:
        manifest_text = self._manifest_provider.read()
        manifest_source = (
            self._manifest_provider.source_url or "the local DepMap manifest"
        )
        rows = parse_manifest(manifest_text, source=manifest_source)
        selection = select_release(rows, release_name=self._release_name)

        release_id = depmap_release_id(self._release_name)
        release = DepMapReleaseMetadata(
            release_id=release_id,
            release_name=selection.release_name,
            release_code=derive_release_code(selection.release_name),
            release_date=selection.release_date,
            manifest_url=self._manifest_provider.source_url,
            manifest_retrieved_at=retrieved_at,
            manifest_local_integrity_hash_algorithm=LOCAL_INTEGRITY_ALGORITHM,
            manifest_local_integrity_hash_value=text_integrity_hash(manifest_text),
        )

        paths: dict[DepMapDataset, Path] = {}
        dataset_files: dict[DepMapDataset, DepMapDatasetFile] = {}
        for dataset in REQUIRED_DATASETS:
            manifest_row = selection.files[dataset]
            path = self._resolver.resolve(
                release_name=selection.release_name, dataset=dataset
            )
            paths[dataset] = path
            dataset_files[dataset] = self._dataset_file(
                dataset, manifest_row, path, release, retrieved_at
            )

        gene_index = parse_gene_metadata(
            self._read_text(paths[DepMapDataset.GENE], DepMapDataset.GENE),
            source=DepMapDataset.GENE.filename,
        )
        models = parse_model_metadata(
            self._read_text(paths[DepMapDataset.MODEL], DepMapDataset.MODEL),
            source=DepMapDataset.MODEL.filename,
        )
        gene_sets: dict[CommonEssentialAnnotationType, ParsedGeneSet] = {}
        for dataset in GENE_SET_DATASETS:
            annotation_type = dataset.annotation_type
            assert annotation_type is not None  # guaranteed by the mapping
            gene_sets[annotation_type] = parse_gene_set(
                self._read_text(paths[dataset], dataset), source=dataset.filename
            )

        model_metadata: list[DepMapModelMetadata] = []
        model_context: dict[str, frozenset[str]] = {}
        for parsed in models:
            model_metadata.append(
                self._model_metadata(parsed, release_id, dataset_files[DepMapDataset.MODEL])
            )
            model_context[parsed.model_id] = frozenset(
                normalize_vocabulary(value) for value in parsed.context_values()
            )

        license_metadata = DataLicenseMetadata(
            source=DEPMAP_SOURCE_NAME,
            release_id=release_id,
            release_name=selection.release_name,
            terms_url=self._terms_url,
            terms_retrieved_at=retrieved_at,
            usage_note=self._usage_note,
            license_note=self._license_note,
        )

        return _ReleaseBundle(
            release=release,
            dataset_files=dataset_files,
            license=license_metadata,
            paths=paths,
            models=tuple(model_metadata),
            model_order=tuple(parsed.model_id for parsed in models),
            model_context=model_context,
            gene_index=gene_index,
            gene_sets=gene_sets,
        )

    def _dataset_file(
        self,
        dataset: DepMapDataset,
        manifest_row: DepMapManifestRow,
        path: Path,
        release: DepMapReleaseMetadata,
        retrieved_at: datetime,
    ) -> DepMapDatasetFile:
        """Validate one acquired file and record both kinds of checksum."""
        local_hash = file_integrity_hash(path)
        source_algorithm: str | None = None
        source_value: str | None = None
        if manifest_row.md5_hash:
            source_algorithm = "MD5"
            source_value = manifest_row.md5_hash.lower()
            actual = file_integrity_hash(path, algorithm="MD5")
            if actual != source_value:
                raise DepMapCollectionError(
                    DepMapFailureReason.SOURCE_CHECKSUM_MISMATCH,
                    f"{dataset.filename!r} for release"
                    f" {release.release_name!r} does not match the checksum the"
                    f" official manifest publishes: expected {source_value},"
                    f" computed {actual}. The local file is not the one this"
                    " release's manifest describes — it may belong to another"
                    " release — so nothing from it is parsed.",
                )
        expected_local_hash = self._resolver.expected_local_integrity_hash(
            release_name=release.release_name, dataset=dataset
        )
        if expected_local_hash is not None and not hmac.compare_digest(
            local_hash, expected_local_hash
        ):
            raise DepMapCollectionError(
                DepMapFailureReason.LOCAL_INTEGRITY_MISMATCH,
                f"{dataset.filename!r} for release {release.release_name!r}"
                " does not match the caller-pinned local file integrity hash:"
                f" expected {expected_local_hash}, computed {local_hash}. This"
                " is a local-file integrity mismatch, not an official checksum"
                " conflict, so nothing from the file is parsed.",
            )
        return DepMapDatasetFile(
            dataset_file_id=depmap_dataset_file_id(
                release.release_name, dataset
            ),
            release_id=release.release_id,
            dataset_name=dataset.value,
            filename=dataset.filename,
            source_checksum_algorithm=source_algorithm,
            source_checksum_value=source_value,
            # Independently named: this is GeneDecision's own hash of the bytes
            # it read, never a checksum the source issued or endorsed.
            local_file_integrity_hash_algorithm=LOCAL_INTEGRITY_ALGORITHM,
            local_file_integrity_hash_value=local_hash,
            official_file_identity=depmap_official_file_identity(
                release.release_name, dataset
            ),
            source_url=manifest_row.durable_url,
            retrieved_at=retrieved_at,
        )

    def _read_text(self, path: Path, dataset: DepMapDataset) -> str:
        try:
            # ``utf-8-sig`` drops a leading byte-order mark, which real DepMap
            # CSV exports commonly carry; a BOM left on the first header cell
            # would make every column name unrecognizable.
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DepMapCollectionError(
                DepMapFailureReason.MALFORMED_DATASET_FILE,
                f"{dataset.filename!r} is not valid UTF-8 text: {exc}",
            ) from exc
        except OSError as exc:
            raise DepMapCollectionError(
                DepMapFailureReason.MISSING_RELEASE_FILE,
                f"{dataset.filename!r} could not be read from {str(path)!r}: {exc}",
            ) from exc

    @staticmethod
    def _model_metadata(
        parsed: ParsedModel,
        release_id: str,
        model_file: DepMapDatasetFile,
    ) -> DepMapModelMetadata:
        return DepMapModelMetadata(
            release_id=release_id,
            model_id=parsed.model_id,
            cell_line_name=parsed.cell_line_name,
            depmap_model_type=parsed.depmap_model_type,
            oncotree_lineage=parsed.oncotree_lineage,
            oncotree_primary_disease=parsed.oncotree_primary_disease,
            oncotree_subtype=parsed.oncotree_subtype,
            source_file_id=model_file.dataset_file_id,
        )

    # -- per-gene collection ----------------------------------------------

    def _headers(
        self, bundle: _ReleaseBundle
    ) -> Mapping[DepMapDataset, MatrixHeaders]:
        """The parsed gene-column headers of both CRISPR matrices, read once."""
        if self._matrix_headers is None:
            self._matrix_headers = {
                dataset: read_matrix_headers(bundle.paths[dataset], dataset=dataset)
                for dataset in MATRIX_DATASETS
            }
        return self._matrix_headers

    def _gene_facts(
        self, bundle: _ReleaseBundle, gene_input: str, retrieved_at: datetime
    ) -> _GeneFacts | None:
        """Collect one gene's factual rows, or ``None`` when it is absent.

        A gene that has no DepMap column is a legitimate ``NO_RESULTS`` outcome,
        never a failure — but both matrices are still read in full first, so a
        malformed file can never masquerade as "DepMap has nothing".
        """
        key = gene_input.strip()
        if key in self._facts_cache:
            return self._facts_cache[key]

        headers = self._headers(bundle)
        read_headers = [headers[dataset] for dataset in MATRIX_DATASETS]
        identity, basis, candidates = resolve_gene_identity(read_headers, key)

        if identity is None:
            for dataset in MATRIX_DATASETS:
                read_matrix(bundle.paths[dataset], dataset=dataset)
            self._facts_cache[key] = None
            return None

        self._validate_identity(bundle, identity)

        reads = {}
        for dataset in MATRIX_DATASETS:
            read = read_matrix(bundle.paths[dataset], dataset=dataset, wanted=identity)
            if read.column_for(identity) is None:
                raise DepMapCollectionError(
                    DepMapFailureReason.INCONSISTENT_MATRIX,
                    f"the selected datasets disagree about {identity.describe()}:"
                    f" {dataset.filename!r} has no column for it while another"
                    " selected matrix does; refusing to collect a partial"
                    " profile",
                )
            reads[dataset] = read

        observations, measured_gene_effect, measured_dependency = (
            self._build_observations(bundle, identity, reads, retrieved_at)
        )
        annotations = self._build_annotations(bundle, identity, retrieved_at)
        facts = _GeneFacts(
            identity=identity,
            evidence_id=self._evidence_id(bundle, identity),
            resolution_basis=basis,
            observations=tuple(observations),
            annotations=tuple(annotations),
            measured_gene_effect=frozenset(measured_gene_effect),
            measured_dependency=frozenset(measured_dependency),
        )
        self._facts_cache[key] = facts
        return facts

    @staticmethod
    def _validate_identity(
        bundle: _ReleaseBundle, identity: GeneColumnIdentity
    ) -> None:
        """Check a matrix column's identity against the release's ``Gene.csv``."""
        entry = bundle.gene_index.validate(identity)
        if entry is None:
            raise DepMapCollectionError(
                DepMapFailureReason.INCONSISTENT_GENE_IDENTITY,
                f"the release's gene metadata does not list"
                f" {identity.describe()}, which a selected CRISPR matrix does;"
                " the release disagrees with itself and the identity cannot be"
                " validated",
            )
        mismatches: list[str] = []
        if (
            identity.entrez_gene_id is not None
            and entry.entrez_gene_id is not None
            and identity.entrez_gene_id != entry.entrez_gene_id
        ):
            mismatches.append(
                f"Entrez {identity.entrez_gene_id} vs {entry.entrez_gene_id}"
            )
        if (
            identity.symbol is not None
            and entry.symbol is not None
            and identity.symbol.casefold() != entry.symbol.casefold()
        ):
            mismatches.append(f"symbol {identity.symbol} vs {entry.symbol}")
        if mismatches:
            raise DepMapCollectionError(
                DepMapFailureReason.INCONSISTENT_GENE_IDENTITY,
                f"the release's gene metadata contradicts a selected CRISPR"
                f" matrix for {identity.raw_header!r}: "
                + "; ".join(mismatches),
            )

    @staticmethod
    def _evidence_id(bundle: _ReleaseBundle, identity: GeneColumnIdentity) -> str:
        identifier = identity.entrez_gene_id or identity.symbol
        assert identifier is not None  # GeneColumnIdentity guarantees one
        return depmap_evidence_id(bundle.release.release_name, identifier)

    def _build_observations(
        self,
        bundle: _ReleaseBundle,
        identity: GeneColumnIdentity,
        reads: Mapping[DepMapDataset, MatrixRead],
        retrieved_at: datetime,
    ) -> tuple[list[DepMapDependencyObservation], set[str], set[str]]:
        """One measurement row per dataset × model, with missing states kept.

        The two measurement types never merge into one row, the model set is
        joined by ``ModelID`` rather than by row position, and a model the
        release's ``Model.csv`` does not describe is preserved and flagged
        rather than dropped.
        """
        evidence_id = self._evidence_id(bundle, identity)
        release_id = bundle.release.release_id
        metadata_models = set(bundle.model_order)
        observations: list[DepMapDependencyObservation] = []
        measured_gene_effect: set[str] = set()
        measured_dependency: set[str] = set()

        for dataset in MATRIX_DATASETS:
            read = reads[dataset]
            measurement_types = dataset.measurement_types
            if len(measurement_types) != 1:
                raise ValueError(
                    f"dataset {dataset.value!r} does not map to exactly one"
                    " measurement type"
                )
            measurement_type = next(iter(measurement_types))
            file_row = bundle.dataset_files[dataset]

            matrix_models = list(read.model_order)
            ordered = list(bundle.model_order) + [
                model_id for model_id in matrix_models if model_id not in metadata_models
            ]
            for model_id in ordered:
                flags: list[str] = []
                if model_id not in read.values:
                    # The model is described by the release but has no row in
                    # this matrix: an expected, recorded absence.
                    status = DepMapMeasurementStatus.MODEL_NOT_IN_MATRIX
                    value: float | None = None
                    raw_value: str | None = None
                else:
                    raw_token = read.values[model_id]
                    value, raw_value = parse_measurement_token(raw_token)
                    if value is None:
                        status = DepMapMeasurementStatus.VALUE_MISSING
                    else:
                        status = DepMapMeasurementStatus.MEASURED
                        if measurement_type is DepMapMeasurementType.GENE_EFFECT:
                            measured_gene_effect.add(model_id)
                        else:
                            measured_dependency.add(model_id)
                    if model_id not in metadata_models:
                        flags.append(FLAG_MODEL_METADATA_UNAVAILABLE)

                observations.append(
                    DepMapDependencyObservation(
                        evidence_id=evidence_id,
                        observation_id=depmap_observation_id(
                            bundle.release.release_name,
                            dataset,
                            model_id,
                            identity.symbol,
                            identity.entrez_gene_id,
                            measurement_type,
                        ),
                        release_id=release_id,
                        dataset_name=dataset.value,
                        source_file_id=file_row.dataset_file_id,
                        source_file_identity=file_row.dataset_file_id,
                        model_id=model_id,
                        gene_symbol=identity.symbol or identity.raw_header,
                        gene_identifier=identity.entrez_gene_id,
                        raw_gene_header=identity.raw_header,
                        measurement_type=measurement_type,
                        measurement_status=status,
                        value=value,
                        raw_value=raw_value,
                        retrieved_at=retrieved_at,
                        quality_flags=flags,
                    )
                )
        return observations, measured_gene_effect, measured_dependency

    @staticmethod
    def _build_annotations(
        bundle: _ReleaseBundle,
        identity: GeneColumnIdentity,
        retrieved_at: datetime,
    ) -> list[CommonEssentialAnnotation]:
        """Record each source-defined essentiality concept separately.

        Membership is written only now, after every list parsed completely and
        the gene identity resolved unambiguously. A list that could not be read
        fails the run instead of being recorded as non-membership, so "absent
        from the file" can never be confused with "the file did not load".
        """
        evidence_id = DepMapConnector._evidence_id(bundle, identity)
        annotations: list[CommonEssentialAnnotation] = []
        for dataset in GENE_SET_DATASETS:
            annotation_type = dataset.annotation_type
            assert annotation_type is not None  # guaranteed by the mapping
            gene_set = bundle.gene_sets[annotation_type]
            annotations.append(
                CommonEssentialAnnotation(
                    evidence_id=evidence_id,
                    release_id=bundle.release.release_id,
                    gene_symbol=identity.symbol or identity.raw_header,
                    gene_identifier=identity.entrez_gene_id,
                    annotation_type=annotation_type,
                    source_dataset=dataset.value,
                    membership_status=(
                        CommonEssentialMembershipStatus.MEMBER
                        if gene_set.contains(identity)
                        else CommonEssentialMembershipStatus.NON_MEMBER
                    ),
                    source_definition=SOURCE_DEFINITIONS[annotation_type],
                    source_file_id=bundle.dataset_files[dataset].dataset_file_id,
                    provenance_note=(
                        f"Membership read from {dataset.filename} of release"
                        f" {bundle.release.release_name!r}; a factual"
                        " source-defined label only."
                    ),
                    retrieved_at=retrieved_at,
                )
            )
        return annotations

    # -- coverage and results ---------------------------------------------

    def _coverage(
        self,
        bundle: _ReleaseBundle,
        query: ConnectorQuery,
        facts: _GeneFacts | None,
    ) -> DepMapContextCoverage:
        """Summarize factual coverage of one query, without interpreting it."""
        measured_gene_effect = (
            facts.measured_gene_effect if facts is not None else frozenset()
        )
        measured_dependency = (
            facts.measured_dependency if facts is not None else frozenset()
        )
        matched: list[str] = []
        if query.disease is None:
            # A phenotype- or drug-only question cannot be matched against the
            # selected files' vocabulary at all. The broad profile is still
            # collected; nothing is discarded.
            state = DepMapCoverageState.NO_MATCHABLE_DISEASE_AXIS
            gene_effect_count = 0
            dependency_count = 0
        else:
            target = normalize_vocabulary(query.disease)
            matched = [
                model_id
                for model_id in bundle.model_order
                if target in bundle.model_context.get(model_id, frozenset())
            ]
            gene_effect_count = sum(
                1 for model_id in matched if model_id in measured_gene_effect
            )
            dependency_count = sum(
                1 for model_id in matched if model_id in measured_dependency
            )
            if not matched:
                state = DepMapCoverageState.NO_EXACT_SOURCE_VOCABULARY_MATCH
            elif gene_effect_count + dependency_count > 0:
                state = DepMapCoverageState.MATCHING_MODELS_MEASURED
            else:
                state = (
                    DepMapCoverageState.MATCHING_MODELS_WITHOUT_RELEVANT_MEASUREMENT
                )

        return DepMapContextCoverage(
            evidence_id=None if facts is None else facts.evidence_id,
            query_text=self.build_query_string(query),
            matching_policy=self._context_match_policy,
            matched_model_count=len(matched),
            matched_models_with_gene_effect_count=gene_effect_count,
            matched_models_with_dependency_probability_count=dependency_count,
            coverage_state=state,
        )

    def _profile(
        self,
        bundle: _ReleaseBundle,
        facts: _GeneFacts,
        query: ConnectorQuery,
        coverage: DepMapContextCoverage,
    ) -> DepMapDependencyMetadata:
        identity = facts.identity
        gene_identifier = identity.entrez_gene_id or identity.symbol
        return DepMapDependencyMetadata(
            evidence_id=facts.evidence_id,
            release=bundle.release,
            dataset_files=list(bundle.ordered_files()),
            license_metadata=bundle.license,
            gene_identity=DepMapGeneIdentity(
                evidence_id=facts.evidence_id,
                input_gene=query.gene.strip(),
                depmap_symbol=identity.symbol or identity.raw_header,
                entrez_gene_id=identity.entrez_gene_id,
                raw_matrix_header=identity.raw_header,
                resolution_basis=facts.resolution_basis,
                gene_metadata_file_id=bundle.dataset_files[
                    DepMapDataset.GENE
                ].dataset_file_id,
            ),
            models=list(bundle.models),
            observations=list(facts.observations),
            common_essential_annotations=list(facts.annotations),
            context_coverage=coverage,
        )

    def _evidence_record(
        self,
        identity: GeneColumnIdentity,
        bundle: _ReleaseBundle,
        retrieved_at: datetime,
    ) -> EvidenceRecord:
        """One release-pinned profile record, and no judgement about it.

        The aggregate leaves every single-value field empty on purpose: picking
        one model or one metric as "the" effect value would privilege or discard
        source data. Disease, phenotype and drug stay ``None`` because they are
        not properties of a baseline dependency profile — the query context that
        asked for it lives in ``QueryMatch``, and model-specific context lives
        in the child observation rows.
        """
        identifier = identity.entrez_gene_id or identity.symbol
        assert identifier is not None  # GeneColumnIdentity guarantees one
        return EvidenceRecord(
            evidence_id=self._evidence_id(bundle, identity),
            gene=identity.symbol or identifier,
            source_scope=SourceScope.PUBLIC,
            source_type=DEPMAP_SOURCE_TYPE,
            source_name=DEPMAP_SOURCE_NAME,
            source_identifier=(
                f"{bundle.release.release_name}:CRISPR:{identifier}"
            ),
            source_url=DEPMAP_SOURCE_URL,
            disease=None,
            phenotype=None,
            drug=None,
            species="Homo sapiens",
            evidence_level=EvidenceLevel.PERTURBATIONAL,
            support=Support.UNKNOWN,
            directness=Directness.UNASSESSED,
            summary=None,
            quality_flags=[FLAG_DEPMAP_MEASUREMENTS_IN_CHILD_ROWS],
            retrieved_at=retrieved_at,
        )

    def _failure(
        self,
        query: ConnectorQuery,
        *,
        reason: DepMapFailureReason,
        message: str,
        retrieved_at: datetime,
        query_string: str | None,
        http_status: int | None = None,
    ) -> DepMapQueryResult:
        """A failure result: no records, no partial profile, exact cause named."""
        return DepMapQueryResult(
            connector=self.name,
            query=query,
            query_string=query_string,
            status=ConnectorStatus.FAILURE,
            retrieved_at=retrieved_at,
            error=ConnectorFailure(
                connector=self.name,
                kind=FAILURE_KINDS[reason],
                message=message,
                http_status=http_status,
            ),
            failure_reason=reason,
        )

    def _no_results(
        self,
        query: ConnectorQuery,
        *,
        query_string: str,
        retrieved_at: datetime,
        reason: DepMapNoDataReason,
        coverage: DepMapContextCoverage,
    ) -> DepMapQueryResult:
        """A clean absence: the release answered and had nothing applicable.

        No record and no profile are created, because there is no measurement to
        attach them to. The release/file provenance is retained at run level and
        the coverage is reported, so an empty answer stays traceable and is
        never presented as evidence that the biology is absent.
        """
        return DepMapQueryResult(
            connector=self.name,
            query=query,
            query_string=query_string,
            status=ConnectorStatus.NO_RESULTS,
            retrieved_at=retrieved_at,
            context_coverage=coverage,
            no_data_reason=reason,
        )
