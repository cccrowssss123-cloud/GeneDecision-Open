"""Normalized evidence schema."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceScope(StrEnum):
    """Whether evidence originates in the user's project or a public source."""

    USER = "USER"
    PUBLIC = "PUBLIC"


class EvidenceLevel(StrEnum):
    """The type of claim supported by an evidence record."""

    PERTURBATIONAL = "PERTURBATIONAL"
    OBSERVATIONAL = "OBSERVATIONAL"
    ASSOCIATIONAL = "ASSOCIATIONAL"
    MECHANISTIC = "MECHANISTIC"
    UNKNOWN = "UNKNOWN"


class Support(StrEnum):
    """Relationship of the record to the current hypothesis."""

    SUPPORT = "SUPPORT"
    CONTRADICT = "CONTRADICT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class Directness(StrEnum):
    """How a record relates to the current gene-disease-phenotype question.

    ``UNASSESSED`` means no such interpretation has been made. It is distinct
    from ``CONTEXT_ONLY``, which is an explicit background-context judgement.
    """

    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    UNASSESSED = "UNASSESSED"


class EvidenceRecord(BaseModel):
    """One normalized item of user or public evidence.

    Optional scientific values remain ``None`` when unavailable; the schema
    never substitutes estimated statistical values.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1)
    gene: str = Field(min_length=1)

    source_scope: SourceScope
    source_type: str = Field(min_length=1)

    source_name: str | None = None
    source_identifier: str | None = None
    source_url: str | None = None

    disease: str | None = None
    phenotype: str | None = None
    drug: str | None = None

    experimental_context: str | None = None
    comparison: str | None = None

    effect_type: str | None = None
    effect_value: float | None = None
    p_value: float | None = None
    fdr: float | None = None
    sample_size: int | None = None

    cell_type: str | None = None
    species: str | None = None

    evidence_level: EvidenceLevel = EvidenceLevel.UNKNOWN
    support: Support = Support.UNKNOWN
    directness: Directness = Directness.UNASSESSED

    summary: str | None = None
    quality_flags: list[str] = Field(default_factory=list)
    retrieved_at: datetime | None = None

    @model_validator(mode="after")
    def public_evidence_has_required_provenance(self) -> "EvidenceRecord":
        """Require the provenance fields the specification mandates for public data."""
        if self.source_scope is SourceScope.PUBLIC:
            missing = [
                name
                for name in ("source_name", "source_identifier", "retrieved_at")
                if getattr(self, name) is None
            ]
            if missing:
                fields = ", ".join(missing)
                raise ValueError(f"PUBLIC evidence requires provenance fields: {fields}")
        return self
