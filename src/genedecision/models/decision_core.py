"""Immutable decision contracts consuming interpretations, never raw science."""

from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer

from genedecision.models.decision import DecisionState
from genedecision.models.evidence import SourceScope
from genedecision.models.interpretation import (
    EvidenceInterpretation, Hypothesis, ProviderType, SourceFieldReference,
    ProjectInterpretationContext,
)

DECISION_SCHEMA_VERSION = "decision-input-v0"
DECISION_RUBRIC_VERSION = "decision-contract-v0"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, revalidate_instances="always")

    @model_serializer(mode="wrap")
    def preserve_historical_shape(self, handler):
        payload = handler(self)
        # Additive v3 fields are absent until supplied. Historical canonical
        # snapshots/candidates retain their original bytes and hashes.
        for name in ("coverage_audits", "grounded_risk_questions", "primary_analysis_kind",
                     "treatment_specificity_tested", "perturbation_specificity_tested",
                     "information_gap_unknown_rationale", "synthesis_contract_version",
                     "synthesis_validation_version", "synthesis_rubric_version"):
            if name in payload and payload[name] in (None, (), []):
                del payload[name]
        return payload


class DecisionAxis(StrEnum):
    INTERNAL_EVIDENCE = "INTERNAL_EVIDENCE"
    INDEPENDENT_EVIDENCE = "INDEPENDENT_EVIDENCE"
    CAUSAL_EVIDENCE = "CAUSAL_EVIDENCE"
    BIOLOGICAL_PLAUSIBILITY = "BIOLOGICAL_PLAUSIBILITY"
    NOVELTY = "NOVELTY"
    CONTRADICTIONS_RISK = "CONTRADICTIONS_RISK"
    INFORMATION_GAP = "INFORMATION_GAP"


class AxisStatus(StrEnum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    ABSENT = "ABSENT"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class SourceKind(StrEnum):
    USER = "USER"
    PUBMED = "PUBMED"
    DEPMAP = "DEPMAP"
    OTHER_PUBLIC = "OTHER_PUBLIC"


class RiskCategory(StrEnum):
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    COMMON_ESSENTIAL_BASELINE_FITNESS = "COMMON_ESSENTIAL_BASELINE_FITNESS"
    NONSPECIFIC_PERTURBATION = "NONSPECIFIC_PERTURBATION"
    TREATMENT_INDEPENDENT_DEPENDENCY = "TREATMENT_INDEPENDENT_DEPENDENCY"
    OPPOSITE_DIRECTION = "OPPOSITE_DIRECTION"
    OBSERVATIONAL_CAUSAL_MISMATCH = "OBSERVATIONAL_CAUSAL_MISMATCH"
    WEAK_REPLICATION = "WEAK_REPLICATION"
    DISEASE_MODEL_MISMATCH = "DISEASE_MODEL_MISMATCH"
    TREATMENT_PHENOTYPE_MISMATCH = "TREATMENT_PHENOTYPE_MISMATCH"
    CELL_COMPOSITION = "CELL_COMPOSITION"
    DUPLICATED_NONINDEPENDENT_EVIDENCE = "DUPLICATED_NONINDEPENDENT_EVIDENCE"
    INSUFFICIENT_COMPARATOR = "INSUFFICIENT_COMPARATOR"
    MISSING_DIRECT_CAUSAL_TEST = "MISSING_DIRECT_CAUSAL_TEST"


class DecisionRelevance(StrEnum):
    BLOCKING = "BLOCKING"
    MATERIAL = "MATERIAL"
    LIMITED = "LIMITED"
    UNASSESSED = "UNASSESSED"


class RiskAssessment(StrEnum):
    PRESENT = "PRESENT"
    UNASSESSED = "UNASSESSED"
    UNRESOLVED = "UNRESOLVED"
    ABSENT = "ABSENT"


class InterventionKind(StrEnum):
    GENETIC = "GENETIC"
    FUNCTIONAL = "FUNCTIONAL"
    LONGITUDINAL_RESPONSE = "LONGITUDINAL_RESPONSE"
    OBSERVATIONAL_ASSOCIATION = "OBSERVATIONAL_ASSOCIATION"
    EXPRESSION_CORRELATION = "EXPRESSION_CORRELATION"
    UNKNOWN = "UNKNOWN"


class ProviderIdentity(FrozenModel):
    provider_type: ProviderType
    provider_name: str = Field(min_length=1)
    provider_version: str | None = Field(default=None, min_length=1)
    model_version: str | None = None
    prompt_version: str | None = None
    rule_version: str | None = None
    reviewer_id: str | None = None


class SourceIdentity(FrozenModel):
    record_type: str = Field(min_length=1)
    record_id: str = Field(min_length=1)


class EvidenceProvenance(FrozenModel):
    evidence_id: str
    source_scope: SourceScope
    source_kind: SourceKind
    source_name: str | None = None
    source_type: str
    source_identifier: str | None = None
    source_records: tuple[SourceIdentity, ...] = ()
    origin_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def scope_matches_kind(self):
        if (self.source_kind is SourceKind.USER) != (self.source_scope is SourceScope.USER):
            raise ValueError("internal/public source identity mismatch")
        return self


class SelectedInterpretation(FrozenModel):
    interpretation: EvidenceInterpretation
    run_id: str
    provider: ProviderIdentity
    interpretation_snapshot_hash: str
    execution_provenance_json: str | None = None
    provenance: EvidenceProvenance

    @model_validator(mode="after")
    def matching_identity(self):
        if self.run_id != self.interpretation.run_id or self.provenance.evidence_id != self.interpretation.evidence_id:
            raise ValueError("selected interpretation provenance mismatch")
        return self


class RiskObservation(FrozenModel):
    category: RiskCategory
    explanation: str = Field(min_length=1)
    severity: DecisionRelevance
    unresolved: bool = True
    source_references: tuple[SourceFieldReference, ...] = Field(min_length=1)
    assessment: RiskAssessment = RiskAssessment.PRESENT


class EvidenceContext(FrozenModel):
    """Caller-supplied, cited factual context; no replacement scientific labels."""
    interpretation_id: str
    independence_group: str | None = None
    modality: str | None = None
    intervention_kind: InterventionKind = InterventionKind.UNKNOWN
    comparator_identified: bool | None = None
    outcome_relevant: bool | None = None
    model_matches: bool | None = None
    treatment_phenotype_matches: bool | None = None
    source_references: tuple[SourceFieldReference, ...] = ()
    risk_observations: tuple[RiskObservation, ...] = ()
    treatment_specificity_tested: bool | None = None
    perturbation_specificity_tested: bool | None = None

    @model_validator(mode="after")
    def facts_require_citations(self):
        facts = (self.independence_group, self.modality, self.comparator_identified,
                 self.outcome_relevant, self.model_matches, self.treatment_phenotype_matches,
                 self.treatment_specificity_tested, self.perturbation_specificity_tested)
        if (any(v is not None for v in facts) or self.intervention_kind is not InterventionKind.UNKNOWN) and not self.source_references:
            raise ValueError("factual decision context requires source references")
        return self


class RedTeamFinding(FrozenModel):
    finding_id: str = Field(min_length=1)
    category: RiskCategory
    explanation: str = Field(min_length=1)
    interpretation_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    severity: DecisionRelevance
    assessment: RiskAssessment
    unresolved: bool
    source_references: tuple[SourceFieldReference, ...] = ()
    resolution_question: str | None = Field(default=None, min_length=1)


class AxisAssessment(FrozenModel):
    axis: DecisionAxis
    status: AxisStatus
    reasoning: str = Field(min_length=1)
    supporting_interpretation_ids: tuple[str, ...] = ()
    contradictory_interpretation_ids: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    unresolved_risks: tuple[str, ...] = Field(default=(), description="Exact RedTeamFinding.finding_id references, never risk prose or category names.")


class EvidenceRoute(StrEnum):
    COMPUTATIONAL_PUBLIC = "COMPUTATIONAL_PUBLIC"
    WET_LAB = "WET_LAB"


class ExpectedImpact(StrEnum):
    COULD_STRENGTHEN = "COULD_STRENGTHEN"
    COULD_WEAKEN = "COULD_WEAKEN"
    COULD_RESOLVE_CONTRADICTION = "COULD_RESOLVE_CONTRADICTION"
    UNKNOWN = "UNKNOWN"


class InformationGap(FrozenModel):
    gap_id: str = Field(min_length=1)
    missing_question: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    evidence_type_needed: str = Field(min_length=1)
    route: EvidenceRoute
    expected_decision_impact: ExpectedImpact
    priority: DecisionRelevance
    actionable: bool
    interpretation_ids: tuple[str, ...] = ()
    primary_analysis_kind: "AnalysisKind | None" = Field(default=None,
        description="V3 selected gaps declare the primary operation needed to answer this question.")


class AnalysisKind(StrEnum):
    STRATIFIED_REANALYSIS = "STRATIFIED_REANALYSIS"
    REPLICATE_VALIDATION = "REPLICATE_VALIDATION"
    COMPARATOR_AWARE_ANALYSIS = "COMPARATOR_AWARE_ANALYSIS"
    ALTERNATIVE_MODEL_CHECK = "ALTERNATIVE_MODEL_CHECK"
    PUBLIC_DATASET_VALIDATION = "PUBLIC_DATASET_VALIDATION"
    DEPENDENCY_CONFOUNDER_ANALYSIS = "DEPENDENCY_CONFOUNDER_ANALYSIS"
    TREATMENT_SPECIFIC_ANALYSIS = "TREATMENT_SPECIFIC_ANALYSIS"
    CELL_TYPE_DECOMPOSITION = "CELL_TYPE_DECOMPOSITION"
    LITERATURE_SOURCE_RETRIEVAL = "LITERATURE_SOURCE_RETRIEVAL"


class NextAnalysis(FrozenModel):
    kind: AnalysisKind
    action: str = Field(min_length=12)
    selection_reason: str = Field(min_length=1)
    gap_ids: tuple[str, ...] = Field(min_length=1)
    interpretation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def specific_action(self):
        if self.action.lower().rstrip(".") in {"do more analysis", "more research needed", "do more research", "analyze more evidence"}:
            raise ValueError("next analysis must specify an actionable computation")
        return self


class NoveltyStatus(StrEnum):
    KNOWN = "KNOWN"
    PARTIALLY_ESTABLISHED = "PARTIALLY_ESTABLISHED"
    POTENTIALLY_NOVEL = "POTENTIALLY_NOVEL"
    UNKNOWN = "UNKNOWN"


class NoveltyAssessment(FrozenModel):
    status: NoveltyStatus = NoveltyStatus.UNKNOWN
    reasoning: str = Field(min_length=1)
    # V1/v2 reserved; v3 references an explicit PublicSearchCoverageAudit.
    search_provenance: tuple[SourceFieldReference, ...] = ()


class PublicSearchExecution(FrozenModel):
    public_source: str = Field(min_length=1)
    query: str = Field(min_length=1)
    searched_at: datetime
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicSearchCoverageAudit(FrozenModel):
    """Caller-admitted audit of a bounded search, not a universal novelty claim."""
    schema_version: Literal["public-search-coverage-audit-v1"] = "public-search-coverage-audit-v1"
    audit_id: str = Field(min_length=1)
    project_name: str
    gene: str
    hypothesis_id: str
    scope: str = Field(min_length=12)
    searches: tuple[PublicSearchExecution, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = Field(min_length=1)
    coverage: Literal["COMPLETE_FOR_DECLARED_SCOPE", "INCOMPLETE"]

    @model_validator(mode="after")
    def bounded_search(self):
        if any(not limitation.strip() for limitation in self.limitations):
            raise ValueError("coverage audit requires explicit nonempty limitations")
        for search in self.searches:
            if search.searched_at.tzinfo is None:
                raise ValueError("coverage audit search time must include a timezone")
        return self

    def provenance(self):
        return SourceFieldReference(record_type="PublicSearchCoverageAudit",
            record_id=self.audit_id, field_name="scope")


class GroundedRiskQuestion(FrozenModel):
    category: RiskCategory
    interpretation_ids: tuple[str, ...] = Field(min_length=1)
    source_references: tuple[SourceFieldReference, ...] = Field(min_length=1)
    resolution_question: str = Field(min_length=12)
    severity: DecisionRelevance

    @model_validator(mode="after")
    def concrete_question(self):
        if self.severity is DecisionRelevance.UNASSESSED:
            raise ValueError("grounded risk question requires qualitative decision relevance")
        if len(set(self.interpretation_ids)) != len(self.interpretation_ids):
            raise ValueError("grounded risk question interpretation IDs must be unique")
        return self


class DecisionInput(FrozenModel):
    schema_version: Literal["decision-input-v0"] = DECISION_SCHEMA_VERSION
    rubric_version: str = Field(default=DECISION_RUBRIC_VERSION, min_length=1)
    project_name: str
    gene: str
    project_context: ProjectInterpretationContext
    hypothesis: Hypothesis
    selected: tuple[SelectedInterpretation, ...] = ()
    factual_context: tuple[EvidenceContext, ...] = ()
    red_team_review: tuple[RedTeamFinding, ...]
    coverage_audits: tuple[PublicSearchCoverageAudit, ...] = ()
    grounded_risk_questions: tuple[GroundedRiskQuestion, ...] = ()
    provider: ProviderIdentity

    @model_validator(mode="after")
    def selection_is_exact(self):
        ids = [s.interpretation.interpretation_id for s in self.selected]
        if self.project_context.project_name != self.project_name or self.project_context.gene != self.gene:
            raise ValueError("decision project context mismatch")
        if len(ids) != len(set(ids)) or ids != sorted(ids):
            raise ValueError("selected interpretation IDs must be unique and sorted")
        if self.hypothesis.project_name != self.project_name or any(
            s.interpretation.project_name != self.project_name or s.interpretation.hypothesis_id != self.hypothesis.hypothesis_id
            for s in self.selected
        ):
            raise ValueError("decision interpretation project/hypothesis mismatch")
        context_ids = [c.interpretation_id for c in self.factual_context]
        if len(set(context_ids)) != len(context_ids) or set(context_ids) - set(ids):
            raise ValueError("factual context must identify selected interpretations uniquely")
        if self.coverage_audits or self.grounded_risk_questions or any(
                c.treatment_specificity_tested is not None or c.perturbation_specificity_tested is not None
                for c in self.factual_context):
            if self.provider.provider_type is not ProviderType.LLM or self.provider.prompt_version != "decision-synthesis-v3":
                raise ValueError("prospective audit/risk input requires decision-synthesis-v3")
        audit_ids = [a.audit_id for a in self.coverage_audits]
        if len(set(audit_ids)) != len(audit_ids):
            raise ValueError("coverage audit IDs must be unique")
        if any((a.project_name, a.gene, a.hypothesis_id) !=
               (self.project_name, self.gene, self.hypothesis.hypothesis_id) for a in self.coverage_audits):
            raise ValueError("coverage audit project/gene/hypothesis must match DecisionInput")
        selected = {s.interpretation.interpretation_id: s for s in self.selected}
        for question in self.grounded_risk_questions:
            if set(question.interpretation_ids) - set(ids):
                raise ValueError("grounded risk question must cite selected interpretations")
            allowed = {r.model_dump_json() for iid in question.interpretation_ids
                       for r in selected[iid].interpretation.rationale_references}
            allowed.update(r.model_dump_json() for c in self.factual_context
                           if c.interpretation_id in question.interpretation_ids
                           for r in c.source_references + tuple(r for o in c.risk_observations for r in o.source_references))
            if any(r.model_dump_json() not in allowed for r in question.source_references):
                raise ValueError("grounded risk question source references must match cited interpretations")
        return self


class GateCondition(FrozenModel):
    condition: str = Field(min_length=1)
    axes: tuple[DecisionAxis, ...] = Field(min_length=1)
    interpretation_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()


class DecisionTrace(FrozenModel):
    gate: str = Field(min_length=1)
    conditions: tuple[GateCondition, ...] = Field(min_length=1)
    next_analysis_reason: str = Field(min_length=1)
    considered_gap_ids: tuple[str, ...] = ()


class DecisionAssessment(FrozenModel):
    decision: DecisionState
    reasoning: str = Field(min_length=1)
    axes: tuple[AxisAssessment, ...]
    red_team_findings: tuple[RedTeamFinding, ...]
    information_gaps: tuple[InformationGap, ...] = ()
    most_decision_changing_next_analysis: NextAnalysis | None = None
    novelty: NoveltyAssessment
    stopping_reason: str | None = None
    synthesis_label: Literal["DETERMINISTIC_BASELINE", "MANUAL_CODEX_DECISION_BRIDGE"] | None = None
    scientific_validation: Literal["SCIENTIFICALLY_UNVALIDATED"] | None = None
    trace: DecisionTrace | None = None

    @model_validator(mode="after")
    def complete_structure(self):
        axes = [a.axis for a in self.axes]
        if len(axes) != 7 or set(axes) != set(DecisionAxis):
            raise ValueError("exactly seven distinct decision axes are required")
        findings = [f.finding_id for f in self.red_team_findings]
        if len(findings) != len(set(findings)) or {f.category for f in self.red_team_findings} != set(RiskCategory):
            raise ValueError("Red Team must cover every category with unique finding IDs")
        gaps = {g.gap_id: g for g in self.information_gaps}
        if len(gaps) != len(self.information_gaps):
            raise ValueError("information gap IDs must be unique")
        for axis in self.axes:
            if set(axis.unresolved_risks) - set(findings):
                raise ValueError("axis risk references must resolve")
        if self.decision is DecisionState.COMPUTE_MORE and self.most_decision_changing_next_analysis is None:
            raise ValueError("COMPUTE_MORE requires actionable next analysis")
        if self.most_decision_changing_next_analysis is not None:
            requested = self.most_decision_changing_next_analysis.gap_ids
            if any(g not in gaps or not gaps[g].actionable or gaps[g].route is not EvidenceRoute.COMPUTATIONAL_PUBLIC for g in requested):
                raise ValueError("next analysis must resolve actionable computational/public gaps")
        if self.decision is DecisionState.DEPRIORITIZE and not self.stopping_reason:
            raise ValueError("DEPRIORITIZE requires an explicit computational stopping reason")
        return self


class DecisionRunStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DecisionExecutionProvenance(FrozenModel):
    provider_type: Literal["LLM"] = "LLM"
    provider_name: Literal["Codex"] = "Codex"
    execution_mode: Literal["MANUAL_CODEX_DECISION_BRIDGE"] = "MANUAL_CODEX_DECISION_BRIDGE"
    transport: Literal["NONE"] = "NONE"
    external_api_call: Literal[False] = False
    simulated_api_path: Literal[True] = True
    provider_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    api_request_id: Literal[None] = None
    api_response_id: Literal[None] = None
    input_token_usage: Literal[None] = None
    output_token_usage: Literal[None] = None
    reasoning_token_usage: Literal[None] = None
    api_latency_ms: Literal[None] = None
    api_cost: Literal[None] = None


class DecisionRun(FrozenModel):
    run_id: str
    project_name: str
    hypothesis_id: str
    provider: ProviderIdentity
    input_snapshot: str
    input_snapshot_hash: str
    snapshot_serialization_version: Literal["canonical-json-v1"] = "canonical-json-v1"
    snapshot_hash_algorithm: Literal["SHA-256"] = "SHA-256"
    started_at: datetime
    completed_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    status: DecisionRunStatus
    error: str | None = None
    execution_provenance: DecisionExecutionProvenance | None = None

    @model_validator(mode="after")
    def lifecycle(self):
        if hashlib.sha256(self.input_snapshot.encode()).hexdigest() != self.input_snapshot_hash:
            raise ValueError("decision snapshot hash mismatch")
        data = DecisionInput.model_validate_json(self.input_snapshot)
        if data.project_name != self.project_name or data.hypothesis.hypothesis_id != self.hypothesis_id or data.provider != self.provider:
            raise ValueError("decision run identity differs from snapshot")
        if self.provider.provider_type is ProviderType.LLM:
            from genedecision.decision.llm import canonical_provider_snapshot
            if (self.execution_provenance is None or self.provider.provider_name != "Codex"
                    or self.execution_provenance.provider_snapshot_hash != canonical_provider_snapshot(data)[1]):
                raise ValueError("LLM execution provenance mismatch")
        elif self.execution_provenance is not None:
            raise ValueError("non-LLM run cannot claim manual execution provenance")
        if self.status is DecisionRunStatus.STARTED:
            if self.completed_at is not None or self.error is not None:
                raise ValueError("STARTED decision cannot have terminal fields")
        elif self.completed_at is None or (self.status is DecisionRunStatus.FAILED) != bool(self.error):
            raise ValueError("terminal decision lifecycle mismatch")
        return self


class DecisionReport(FrozenModel):
    """Engine report v0; the legacy presentation DecisionReport is unchanged."""
    schema_version: Literal["decision-report-v0"] = "decision-report-v0"
    report_id: str
    run_id: str
    project_name: str
    hypothesis_id: str
    input_snapshot_hash: str
    selected_interpretation_ids: tuple[str, ...]
    assessment: DecisionAssessment
    provider_candidate_json: str | None = None
    created_at: datetime


def canonical_decision_snapshot(data: DecisionInput) -> tuple[str, str]:
    payload = data.model_dump(mode="json")
    payload["factual_context"].sort(key=lambda c: c["interpretation_id"])
    snapshot = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return snapshot, hashlib.sha256(snapshot.encode()).hexdigest()


def validate_decision_assessment(data: DecisionInput, result: DecisionAssessment) -> None:
    """Structural scientific guards, not validation of biological decision quality."""
    selected = {s.interpretation.interpretation_id: s for s in data.selected}
    ids = set(selected)
    evidence_ids = {s.provenance.evidence_id for s in data.selected}
    for axis in result.axes:
        refs = set(axis.supporting_interpretation_ids + axis.contradictory_interpretation_ids)
        if refs - ids:
            raise ValueError("axis claims must resolve to selected interpretations")
        if axis.axis in (DecisionAxis.INTERNAL_EVIDENCE, DecisionAxis.INDEPENDENT_EVIDENCE):
            expected = SourceScope.USER if axis.axis is DecisionAxis.INTERNAL_EVIDENCE else SourceScope.PUBLIC
            if any(selected[i].provenance.source_scope is not expected for i in refs):
                raise ValueError("internal/public axis provenance mismatch")
        if axis.axis in (DecisionAxis.INTERNAL_EVIDENCE, DecisionAxis.INDEPENDENT_EVIDENCE, DecisionAxis.CAUSAL_EVIDENCE):
            if any(selected[i].interpretation.support.value != "SUPPORT" for i in axis.supporting_interpretation_ids):
                raise ValueError("directional axis support requires SUPPORT interpretations")
        if any(selected[i].interpretation.support.value != "CONTRADICT" for i in axis.contradictory_interpretation_ids):
            raise ValueError("contradictory claims require CONTRADICT interpretations")
        if axis.status in (AxisStatus.STRONG, AxisStatus.MODERATE, AxisStatus.WEAK, AxisStatus.MIXED) and axis.axis in (
            DecisionAxis.INTERNAL_EVIDENCE, DecisionAxis.INDEPENDENT_EVIDENCE,
            DecisionAxis.CAUSAL_EVIDENCE, DecisionAxis.BIOLOGICAL_PLAUSIBILITY,
        ) and not refs:
            raise ValueError("nonempty evidence status requires selected evidence")
    findings = {f.finding_id: f for f in result.red_team_findings}
    gaps = {g.gap_id for g in result.information_gaps}
    if result.trace:
        for condition in result.trace.conditions:
            if (set(condition.interpretation_ids) - ids or set(condition.finding_ids) - set(findings)
                    or set(condition.gap_ids) - gaps):
                raise ValueError("decision trace provenance must resolve")
        if set(result.trace.considered_gap_ids) - gaps:
            raise ValueError("decision trace gap provenance must resolve")
    if data.provider.provider_name == "genedecision-deterministic":
        if (result.trace is None or result.synthesis_label != "DETERMINISTIC_BASELINE"
                or result.scientific_validation != "SCIENTIFICALLY_UNVALIDATED"):
            raise ValueError("deterministic baseline requires trace and unvalidated labels")
        gates = {
            DecisionState.PRIORITIZE: "GROUNDED_SUPPORT_WITHOUT_DECISION_CHANGING_GAP",
            DecisionState.COMPUTE_MORE: "RESOLVE_MATERIAL_GAP_OR_CONFLICT",
            DecisionState.DEPRIORITIZE: "DIRECT_CONTRADICTION_DOMINATES",
        }
        if (result.trace.gate != gates[result.decision]
                or {a for c in result.trace.conditions for a in c.axes} != set(DecisionAxis)
                or set(result.trace.considered_gap_ids) != gaps):
            raise ValueError("deterministic decision trace must cover gate, axes and gaps")
    for finding in result.red_team_findings:
        if set(finding.interpretation_ids) - ids or set(finding.evidence_ids) - evidence_ids:
            raise ValueError("Red Team provenance must resolve to selected evidence")
        if set(finding.evidence_ids) != {selected[i].provenance.evidence_id for i in finding.interpretation_ids}:
            raise ValueError("Red Team evidence IDs differ from cited interpretations")
        if finding.assessment is RiskAssessment.PRESENT and not finding.interpretation_ids and finding.category is not RiskCategory.MISSING_DIRECT_CAUSAL_TEST:
            raise ValueError("asserted confounder requires evidence provenance")
        supplied_refs = {
            ref.model_dump_json() for iid in finding.interpretation_ids
            for ref in selected[iid].interpretation.rationale_references
        }
        supplied_refs.update(ref.model_dump_json() for c in data.factual_context
                            if c.interpretation_id in finding.interpretation_ids
                            for ref in c.source_references + tuple(r for o in c.risk_observations for r in o.source_references))
        if any(ref.model_dump_json() not in supplied_refs for ref in finding.source_references):
            raise ValueError("Red Team source references must resolve to cited provenance")
        if finding.assessment is RiskAssessment.ABSENT and (finding.unresolved or not finding.source_references):
            raise ValueError("risk absence requires resolved cited evidence")
    for required in data.red_team_review:
        if findings.get(required.finding_id) != required:
            raise ValueError("provider cannot omit or rewrite the input Red Team review")
    for gap in result.information_gaps:
        if set(gap.interpretation_ids) - ids:
            raise ValueError("information gap references unselected interpretations")
    if result.most_decision_changing_next_analysis and set(result.most_decision_changing_next_analysis.interpretation_ids) - ids:
        raise ValueError("next analysis references unselected interpretations")
    baseline = data.provider.provider_name == "genedecision-deterministic"
    exact_public = [s for s in data.selected if s.provenance.source_scope is SourceScope.PUBLIC
                    and s.interpretation.directness.value == "DIRECT"
                    and s.provenance.source_kind is SourceKind.PUBMED
                    and any(k.startswith("PMID:") for k in s.provenance.origin_keys)
                    and any(c.interpretation_id == s.interpretation.interpretation_id
                            and c.outcome_relevant is True and c.model_matches is True
                            and c.treatment_phenotype_matches is True for c in data.factual_context)]
    v3 = data.provider.provider_type is ProviderType.LLM and data.provider.prompt_version == "decision-synthesis-v3"
    if not v3 and ((result.novelty.status is not NoveltyStatus.UNKNOWN and not
            (baseline and exact_public and result.novelty.status is NoveltyStatus.PARTIALLY_ESTABLISHED)) or result.novelty.search_provenance):
        raise ValueError("v0 novelty remains UNKNOWN without a public-search coverage audit")
    axes = {a.axis: a for a in result.axes}
    if not v3 and axes[DecisionAxis.NOVELTY].status is not AxisStatus.UNKNOWN and not (
            baseline and exact_public and result.novelty.status is NoveltyStatus.PARTIALLY_ESTABLISHED
            and axes[DecisionAxis.NOVELTY].status is AxisStatus.MODERATE):
        raise ValueError("v0 novelty axis must remain UNKNOWN")
    contexts = {c.interpretation_id: c for c in data.factual_context}
    if result.decision is DecisionState.PRIORITIZE:
        candidates = set(axes[DecisionAxis.CAUSAL_EVIDENCE].supporting_interpretation_ids)
        qualified = [i for i in candidates if selected[i].interpretation.support.value == "SUPPORT"
                     and selected[i].interpretation.directness.value == "DIRECT"
                     and selected[i].interpretation.design_type.value == "PERTURBATIONAL"
                     and i in contexts and contexts[i].comparator_identified is True
                     and contexts[i].outcome_relevant is True and contexts[i].model_matches is True
                     and contexts[i].treatment_phenotype_matches is True
                     and contexts[i].intervention_kind in (InterventionKind.GENETIC, InterventionKind.FUNCTIONAL)]
        if not qualified or not any(set(qualified) & set(axes[a].supporting_interpretation_ids) for a in (
            DecisionAxis.INTERNAL_EVIDENCE, DecisionAxis.INDEPENDENT_EVIDENCE)):
            raise ValueError("PRIORITIZE requires grounded direct causal support beyond plausibility")
        if any(f.unresolved and f.assessment in (RiskAssessment.PRESENT, RiskAssessment.UNRESOLVED)
               and f.severity is DecisionRelevance.BLOCKING for f in result.red_team_findings):
            raise ValueError("PRIORITIZE cannot ignore blocking Red Team findings")
        if any(g.actionable and g.priority is DecisionRelevance.BLOCKING for g in result.information_gaps):
            raise ValueError("PRIORITIZE cannot ignore blocking information gaps")
        if baseline and any(g.actionable and g.route is EvidenceRoute.COMPUTATIONAL_PUBLIC
                            and g.priority is DecisionRelevance.MATERIAL for g in result.information_gaps):
            raise ValueError("deterministic PRIORITIZE cannot ignore material computational gaps")
    if result.decision is DecisionState.DEPRIORITIZE:
        contradiction_ids = axes[DecisionAxis.CONTRADICTIONS_RISK].contradictory_interpretation_ids
        if baseline and not any(selected[i].interpretation.directness.value == "DIRECT"
            and selected[i].interpretation.design_type.value == "PERTURBATIONAL"
            and i in contexts and contexts[i].intervention_kind in (InterventionKind.GENETIC, InterventionKind.FUNCTIONAL)
            and all(getattr(contexts[i], field) is True for field in (
                "comparator_identified", "outcome_relevant", "model_matches", "treatment_phenotype_matches"))
            for i in contradiction_ids):
            raise ValueError("deterministic DEPRIORITIZE requires qualified target contradiction")
        if not any(selected[i].interpretation.support.value == "CONTRADICT"
                   and selected[i].interpretation.directness.value == "DIRECT" for i in contradiction_ids):
            raise ValueError("DEPRIORITIZE requires direct contradiction, not missing evidence")
        if axes[DecisionAxis.CONTRADICTIONS_RISK].status not in (AxisStatus.STRONG, AxisStatus.MIXED):
            raise ValueError("DEPRIORITIZE requires an explicit material contradiction assessment")
        if any(g.actionable and g.route is EvidenceRoute.COMPUTATIONAL_PUBLIC and g.priority in (
            DecisionRelevance.BLOCKING, DecisionRelevance.MATERIAL) for g in result.information_gaps):
            raise ValueError("resolvable decision-changing gaps require COMPUTE_MORE")
