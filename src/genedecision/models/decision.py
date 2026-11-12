"""Structured, traceable GeneDecision report schema."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from genedecision.models.evidence import EvidenceRecord


class DecisionState(StrEnum):
    """The only final classifications permitted by the specification."""

    PRIORITIZE = "PRIORITIZE"
    COMPUTE_MORE = "COMPUTE_MORE"
    DEPRIORITIZE = "DEPRIORITIZE"


class WetLabReadiness(StrEnum):
    """The report's pre-wet-lab readiness labels."""

    READY_FOR_FUNCTIONAL_VALIDATION = "READY FOR FUNCTIONAL VALIDATION"
    NOT_READY_COMPUTATIONAL_GAP = "NOT READY — COMPUTATIONAL GAP"
    LOW_PRIORITY = "LOW PRIORITY"


class EvidenceAxis(BaseModel):
    """Transparent output shared by each independent evidence axis."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: str = Field(min_length=1)
    reasoning_summary: str = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradictory_evidence_ids: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class EvidenceAxes(BaseModel):
    """The seven independent evidence axes required for a final decision."""

    model_config = ConfigDict(extra="forbid")

    internal_evidence: EvidenceAxis
    independent_evidence: EvidenceAxis
    causal_evidence: EvidenceAxis
    biological_plausibility: EvidenceAxis
    novelty: EvidenceAxis
    contradictions_risk: EvidenceAxis
    information_gap: EvidenceAxis


class DecisionSummary(BaseModel):
    """Separates evidence, interpretation, and hypothesis in report prose."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence: str = Field(min_length=1)
    interpretation: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)


class NoveltyAssessment(BaseModel):
    """Novelty evaluated at each context level required by the report."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    gene_in_cancer: str = Field(min_length=1)
    gene_in_disease: str = Field(min_length=1)
    gene_in_phenotype_or_drug: str = Field(min_length=1)
    exact_gene_disease_phenotype_relationship: str = Field(min_length=1)


class DecisionReport(BaseModel):
    """The fixed, evidence-traceable GeneDecision report structure."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    gene: str = Field(min_length=1)
    disease: str = Field(min_length=1)
    phenotype: str = Field(min_length=1)
    decision: DecisionState
    research_question: str = Field(min_length=1)

    decision_summary: DecisionSummary
    evidence_axes: EvidenceAxes

    supporting_evidence: list[EvidenceRecord] = Field(default_factory=list)
    contradictory_evidence: list[EvidenceRecord] = Field(default_factory=list)
    novelty_assessment: NoveltyAssessment

    most_important_uncertainties: list[str] = Field(min_length=1, max_length=3)
    most_valuable_next_computational_analysis: str = Field(min_length=1)
    expected_decision_impact: str = Field(min_length=1)
    wet_lab_readiness: WetLabReadiness

    sources_successfully_queried: list[str] = Field(default_factory=list)
    sources_unavailable: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def axis_references_are_traceable(self) -> "DecisionReport":
        """Ensure every axis citation resolves to an included evidence record."""
        available_ids = {
            record.evidence_id
            for record in self.supporting_evidence + self.contradictory_evidence
        }
        axes = (
            self.evidence_axes.internal_evidence,
            self.evidence_axes.independent_evidence,
            self.evidence_axes.causal_evidence,
            self.evidence_axes.biological_plausibility,
            self.evidence_axes.novelty,
            self.evidence_axes.contradictions_risk,
            self.evidence_axes.information_gap,
        )
        referenced_ids = {
            evidence_id
            for axis in axes
            for evidence_id in (
                axis.supporting_evidence_ids + axis.contradictory_evidence_ids
            )
        }
        missing_ids = sorted(referenced_ids - available_ids)
        if missing_ids:
            raise ValueError(
                "Evidence axis references must resolve to included EvidenceRecords: "
                + ", ".join(missing_ids)
            )
        return self
