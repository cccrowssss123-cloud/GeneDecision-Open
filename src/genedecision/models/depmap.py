"""DepMap-specific factual models.

The generic :class:`~genedecision.models.evidence.EvidenceRecord` is the shared
evidence contract, but DepMap reports several things it has no column for: which
release a number came from, the exact dataset and file identity behind it, the
model it belongs to, and the source's own essentiality membership lists. Those
live here as typed source-specific models — the same pattern
:class:`~genedecision.connectors.base.PublicationMetadata` uses for PubMed —
rather than as JSON blobs on the evidence record or as DepMap-only columns on a
generic table.

Every model in this module records **what the source said**. None of them
carries a judgement about it:

* gene effect and dependency probability are separate factual measurements, and
  neither is thresholded, ranked, averaged or combined;
* there is no ``is_common_essential`` boolean — the source defines three
  different essentiality concepts and all three are preserved independently;
* a missing measurement is stored as missing, never as zero;
* no model states whether a dependency is broad, phenotype-specific, expected,
  or relevant to any hypothesis.

Those questions belong to a future Interpretation / Hypothesis Red Team layer,
which is not implemented here.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Name recorded as ``EvidenceRecord.source_name`` for this source.
DEPMAP_SOURCE_NAME = "DepMap"
#: ``EvidenceRecord.source_type`` for a release-pinned dependency profile.
DEPMAP_SOURCE_TYPE = "CRISPR_DEPENDENCY_PROFILE"
#: Stable landing page for DepMap's downloadable data.
DEPMAP_SOURCE_URL = "https://depmap.org/portal/data_page/?tab=allData"
#: Canonical official terms reference recorded as licensing provenance.
DEPMAP_TERMS_URL = "https://depmap.org/portal/terms/"


class DepMapMeasurementType(StrEnum):
    """Which dataset a measurement came from.

    The two selected CRISPR datasets measure different things, so they are two
    measurement types rather than two columns of one row. One may exist for a
    gene/model while the other does not, and neither may be imputed from the
    other.
    """

    GENE_EFFECT = "GENE_EFFECT"
    DEPENDENCY_PROBABILITY = "DEPENDENCY_PROBABILITY"


class DepMapMeasurementStatus(StrEnum):
    """Why a gene/model cell does or does not carry a number.

    ``MEASURED`` is a parsed finite number. The two missing states are kept
    apart because they mean different things about the source: the model has no
    row in the matrix at all, or it has a row and the cell is explicitly empty.
    """

    MEASURED = "MEASURED"
    MODEL_NOT_IN_MATRIX = "MODEL_NOT_IN_MATRIX"
    VALUE_MISSING = "VALUE_MISSING"


class CommonEssentialAnnotationType(StrEnum):
    """The distinct source-defined essentiality concepts.

    These are not interchangeable labels for one idea. DepMap defines a
    screen-derived inferred set and two reference/control sets, and a gene may
    legitimately have different membership under each.
    """

    CRISPR_INFERRED_COMMON_ESSENTIAL = "CRISPR_INFERRED_COMMON_ESSENTIAL"
    REFERENCE_ESSENTIAL_CONTROL = "REFERENCE_ESSENTIAL_CONTROL"
    REFERENCE_NONESSENTIAL_CONTROL = "REFERENCE_NONESSENTIAL_CONTROL"


class CommonEssentialMembershipStatus(StrEnum):
    """A membership fact read out of one complete, validated source list."""

    MEMBER = "MEMBER"
    NON_MEMBER = "NON_MEMBER"


class GeneResolutionBasis(StrEnum):
    """How the caller's gene input was matched against DepMap's own identity."""

    EXACT_SYMBOL = "EXACT_SYMBOL"
    EXPLICIT_ENTREZ = "EXPLICIT_ENTREZ"


class DepMapCoverageState(StrEnum):
    """What one query's factual coverage of the collected profile looks like."""

    MATCHING_MODELS_MEASURED = "MATCHING_MODELS_MEASURED"
    MATCHING_MODELS_WITHOUT_RELEVANT_MEASUREMENT = (
        "MATCHING_MODELS_WITHOUT_RELEVANT_MEASUREMENT"
    )
    NO_EXACT_SOURCE_VOCABULARY_MATCH = "NO_EXACT_SOURCE_VOCABULARY_MATCH"
    NO_MATCHABLE_DISEASE_AXIS = "NO_MATCHABLE_DISEASE_AXIS"


class DepMapNoDataReason(StrEnum):
    """Why a successfully validated DepMap query returned no measurements.

    ``NO_RESULTS`` is not one state. The gene may be absent from the selected
    dataset, or it may be present while the selected datasets carry no numeric
    measurement for it at all. Both remain distinguishable from a failure.
    """

    GENE_NOT_PRESENT_IN_SELECTED_DATASET = "GENE_NOT_PRESENT_IN_SELECTED_DATASET"
    NO_APPLICABLE_MEASUREMENT_FOR_RESOLVED_GENE = (
        "NO_APPLICABLE_MEASUREMENT_FOR_RESOLVED_GENE"
    )


class DepMapFailureReason(StrEnum):
    """The precise cause behind a DepMap ``FAILURE`` result.

    The generic ``ConnectorFailureKind`` stays coarse (transport, HTTP,
    malformed response, invalid query); the DepMap-specific reason is recorded
    separately so a source-checksum conflict is never confused with a local
    cache-integrity change, and a missing release file is never confused with a
    malformed one.
    """

    INVALID_QUERY = "INVALID_QUERY"
    MANIFEST_UNAVAILABLE = "MANIFEST_UNAVAILABLE"
    MANIFEST_MALFORMED = "MANIFEST_MALFORMED"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
    MISSING_RELEASE_FILE = "MISSING_RELEASE_FILE"
    SOURCE_CHECKSUM_MISMATCH = "SOURCE_CHECKSUM_MISMATCH"
    LOCAL_INTEGRITY_MISMATCH = "LOCAL_INTEGRITY_MISMATCH"
    MALFORMED_DATASET_FILE = "MALFORMED_DATASET_FILE"
    INCONSISTENT_MATRIX = "INCONSISTENT_MATRIX"
    INCONSISTENT_GENE_IDENTITY = "INCONSISTENT_GENE_IDENTITY"
    GENE_IDENTITY_AMBIGUOUS = "GENE_IDENTITY_AMBIGUOUS"


class DepMapReleaseMetadata(BaseModel):
    """The pinned DepMap release one collection run belongs to.

    ``manifest_local_integrity_hash_*`` is a value GeneDecision computed over
    the manifest bytes it read. DepMap does not publish an authoritative
    checksum for the manifest itself, so the field name says ``local`` and the
    value is never described as source-issued or source-validated.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    release_id: str = Field(min_length=1)
    release_name: str = Field(min_length=1)
    release_code: str | None = None
    release_date: str | None = None
    manifest_url: str | None = None
    manifest_retrieved_at: datetime
    manifest_local_integrity_hash_algorithm: str | None = None
    manifest_local_integrity_hash_value: str | None = None

    @model_validator(mode="after")
    def integrity_hash_is_complete(self) -> "DepMapReleaseMetadata":
        fields = (
            self.manifest_local_integrity_hash_algorithm,
            self.manifest_local_integrity_hash_value,
        )
        if (fields[0] is None) != (fields[1] is None):
            raise ValueError(
                "a local integrity hash must carry both its algorithm and its"
                " value, or neither"
            )
        return self


class DepMapDatasetFile(BaseModel):
    """One official release file and its provenance.

    ``source_checksum_*`` holds a checksum DepMap published through the official
    manifest, validated before parsing. ``local_file_integrity_hash_*`` holds a
    hash GeneDecision computed itself for local reproducibility and cache
    integrity. They are not interchangeable: a local hash is never described as
    official checksum validation.

    ``official_file_identity`` carries the durable identity
    (release + dataset + filename) so provenance survives when no durable file
    URL exists; temporary signed query parameters are never stored as the
    source identifier.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dataset_file_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    dataset_name: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    source_checksum_algorithm: str | None = None
    source_checksum_value: str | None = None
    local_file_integrity_hash_algorithm: str | None = None
    local_file_integrity_hash_value: str | None = None
    official_file_identity: str = Field(min_length=1)
    source_url: str | None = None
    retrieved_at: datetime

    @model_validator(mode="after")
    def checksum_pairs_are_complete(self) -> "DepMapDatasetFile":
        if (self.source_checksum_algorithm is None) != (
            self.source_checksum_value is None
        ):
            raise ValueError(
                "a source checksum must carry both its algorithm and its value,"
                " or neither"
            )
        if (self.local_file_integrity_hash_algorithm is None) != (
            self.local_file_integrity_hash_value is None
        ):
            raise ValueError(
                "a local file integrity hash must carry both its algorithm and"
                " its value, or neither"
            )
        return self


class DataLicenseMetadata(BaseModel):
    """Provenance for the terms reference that accompanied a collection run.

    This records which official terms URL was current when the release was
    imported, and the factual notes attached to that import. It grants no
    rights, contains no inferred permission boolean such as
    ``commercial_use_allowed``, and replaces no human legal review. Connector
    support for DepMap does not imply commercial redistribution rights.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    release_name: str = Field(min_length=1)
    terms_url: str | None = None
    terms_retrieved_at: datetime | None = None
    usage_note: str | None = None
    license_note: str | None = None


class DepMapGeneIdentity(BaseModel):
    """How a caller's gene input resolved against DepMap's own identity.

    The raw matrix header is preserved alongside the parsed symbol and embedded
    identifier, so the exact source spelling remains inspectable. No alias,
    retired symbol or synonym is ever substituted; ``input_gene`` is what the
    caller wrote.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1)
    input_gene: str = Field(min_length=1)
    depmap_symbol: str = Field(min_length=1)
    entrez_gene_id: str | None = None
    raw_matrix_header: str = Field(min_length=1)
    resolution_basis: GeneResolutionBasis
    gene_metadata_file_id: str = Field(min_length=1)


class DepMapModelMetadata(BaseModel):
    """The minimum release-matched model identity and context.

    Only the fields needed to identify a model and preserve its factual
    disease/lineage vocabulary are normalized. A ``Model.csv`` column the
    release does not carry stays ``None``; nothing is invented because the
    contract mentions the concept.

    The connector does not infer disease subtypes from broad lineage labels.
    Context normalization belongs to a later layer.
    """

    # Context fields preserve the source spelling and whitespace. Exact
    # comparison uses a separate technical normalization key in the connector.
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    cell_line_name: str | None = None
    depmap_model_type: str | None = None
    oncotree_lineage: str | None = None
    oncotree_primary_disease: str | None = None
    oncotree_subtype: str | None = None
    source_file_id: str = Field(min_length=1)


class DepMapDependencyObservation(BaseModel):
    """Exactly one dataset measurement for one gene × model × release.

    Gene effect and dependency probability are always two rows, even for the
    same gene and model, because they come from two datasets with two file
    identities. ``value`` is the parsed finite number for a ``MEASURED`` row and
    ``None`` for every missing state — a missing measurement is never imputed,
    never filled with zero, and never derived from the other measurement type.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    dataset_name: str = Field(min_length=1)
    source_file_id: str = Field(min_length=1)
    source_file_identity: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    gene_symbol: str = Field(min_length=1)
    gene_identifier: str | None = None
    raw_gene_header: str = Field(min_length=1)
    measurement_type: DepMapMeasurementType
    measurement_status: DepMapMeasurementStatus
    value: float | None = None
    raw_value: str | None = None
    retrieved_at: datetime
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def missing_is_not_zero(self) -> "DepMapDependencyObservation":
        if self.measurement_status is DepMapMeasurementStatus.MEASURED:
            if self.value is None:
                raise ValueError(
                    f"observation {self.observation_id!r} is MEASURED but has no"
                    " value"
                )
            if not math.isfinite(self.value):
                raise ValueError(
                    f"observation {self.observation_id!r} is MEASURED but its"
                    " value is not finite"
                )
        elif self.value is not None:
            raise ValueError(
                f"observation {self.observation_id!r} is"
                f" {self.measurement_status.value} but carries a value; a"
                " missing measurement is never imputed"
            )

        if self.measurement_status is DepMapMeasurementStatus.MODEL_NOT_IN_MATRIX:
            if self.raw_value is not None:
                raise ValueError(
                    f"observation {self.observation_id!r} is MODEL_NOT_IN_MATRIX,"
                    " so its matrix has no row and therefore no source token"
                )
        elif self.raw_value is None:
            raise ValueError(
                f"observation {self.observation_id!r} is"
                f" {self.measurement_status.value} but does not record the source"
                " token it was read from"
            )
        return self


class CommonEssentialAnnotation(BaseModel):
    """One source-defined essentiality membership fact.

    Each source list is written as its own annotation with its own dataset,
    file and source definition. Membership values across the three definitions
    are not reconciled, because they answer different source questions rather
    than disagreeing about one.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    gene_symbol: str = Field(min_length=1)
    gene_identifier: str | None = None
    annotation_type: CommonEssentialAnnotationType
    source_dataset: str = Field(min_length=1)
    membership_status: CommonEssentialMembershipStatus
    source_definition: str = Field(min_length=1)
    source_file_id: str = Field(min_length=1)
    provenance_note: str | None = None
    retrieved_at: datetime
