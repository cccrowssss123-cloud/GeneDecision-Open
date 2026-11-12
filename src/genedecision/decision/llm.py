"""Decision synthesis contract. No network, SDK, raw evidence or benchmark answers."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator

from genedecision.decision.families import normalize_synthesis
from genedecision.models.decision import DecisionState
from genedecision.models.decision_core import (
    FrozenModel, DecisionInput, DecisionAssessment, AxisAssessment, RedTeamFinding,
    InformationGap, NextAnalysis, NoveltyAssessment, SelectedInterpretation,
    EvidenceContext, ProviderIdentity, RiskAssessment,
    validate_decision_assessment, PublicSearchCoverageAudit, GroundedRiskQuestion,
)
from genedecision.models.evidence import SourceScope
from genedecision.models.interpretation import Hypothesis, ProjectInterpretationContext, SourceFieldReference

MODE = "MANUAL_CODEX_DECISION_BRIDGE"
RUBRIC = """Synthesize the confirmed hypothesis from ONLY these selected persisted
interpretations and cited factual context. Never reinterpret raw evidence, change
labels, invent literature/results/statistics, or predict future results.
PRIORITIZE: coherent relevant evidence justifies wet-lab validation now, without
asserting truth or success. Plausibility alone is insufficient.
COMPUTE_MORE: exactly one specific obtainable computational/public task could
change investment readiness. Identify linked uncertainty, motivation, and an
expected effect category through the linked gap, never a predicted result.
DEPRIORITIZE: existing direct contradiction makes further pre-wet-lab computation
unlikely to make the candidate competitive; missing evidence alone is insufficient.
Seven axes and all thirteen Red Team categories are mandatory. Retain every
factual Red Team input finding unchanged. You may add evidence-grounded findings
or explicitly UNRESOLVED questions under new unique IDs. PRESENT requires cited
interpretations and source fields; ABSENT requires an explicitly cited absence
observation. UNRESOLVED requires a resolution_question. No supplied evidence does
not imply absence. Distinct rows do not prove independence. Preserve USER versus
PUBLIC scope. Novelty remains UNKNOWN without a coverage audit. Use exact selected
IDs and admitted source references; list precisely the interpretations referenced
anywhere in the output in used_interpretations. Confidence is null. No overall
score or wet-lab probability. These guards are structural, not scientific validation.
""".strip()

DEFAULT_PROMPT_VERSION = "decision-synthesis-v3"
SUPPORTED_CONTRACT_VERSIONS = ("decision-synthesis-v1", "decision-synthesis-v2", DEFAULT_PROMPT_VERSION)
VALIDATION_V3 = "decision-output-validation-v3"
RUBRIC_V3_VERSION = "decision-synthesis-rubric-v3"
RUBRIC_V2 = RUBRIC + """
Field formatting: axes[].unresolved_risks contains exact RedTeamFinding.finding_id
strings only, never prose or category names. Every interpretation citation uses
selected[].interpretation.interpretation_id, never evidence_id. INTERNAL_EVIDENCE
citations are USER and INDEPENDENT_EVIDENCE citations PUBLIC. Directional support
on these axes and CAUSAL_EVIDENCE requires SUPPORT; contradictory citations always
require CONTRADICT. Evidence axes with STRONG/MODERATE/WEAK/MIXED need citations.
Retain the factual Red Team input exactly, including source references and flags.
used_interpretations exactly covers the union of every output interpretation ID.
""".rstrip()


def synthesis_contract(version):
    # Historical contracts remain pinned even as model documentation evolves.
    root = Path(__file__).with_name("schemas")
    versions = {"decision-synthesis-v1": "v1", "decision-synthesis-v2": "v2", "decision-synthesis-v3": "v3"}
    if version in versions:
        suffix = versions[version]
        return (root.joinpath(f"rubric-{suffix}.txt").read_text(),
                root.joinpath(f"candidate-{suffix}.json").read_text().strip())
    raise ValueError("unsupported decision synthesis contract version")


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class UsedInterpretation(FrozenModel):
    interpretation_id: str
    evidence_id: str
    source_scope: SourceScope
    source_references: tuple[SourceFieldReference, ...] = Field(min_length=1)


class LLMDecisionCandidate(FrozenModel):
    """Runtime scientific content only; service supplies identity and execution data."""
    final_decision: DecisionState
    axes: tuple[AxisAssessment, ...]
    red_team_findings: tuple[RedTeamFinding, ...]
    information_gaps: tuple[InformationGap, ...] = ()
    most_decision_changing_next_analysis: NextAnalysis | None = None
    reasoning: str = Field(min_length=1)
    novelty: NoveltyAssessment
    stopping_reason: str | None = None
    used_interpretations: tuple[UsedInterpretation, ...]
    confidence: Literal[None] = None
    information_gap_unknown_rationale: str | None = Field(default=None, min_length=12,
        description="V3 exceptional reason why a MATERIAL/BLOCKING COMPUTE_MORE gap cannot support an INFORMATION_GAP assessment.")

    def assessment(self):
        return DecisionAssessment(decision=self.final_decision, axes=self.axes,
            red_team_findings=self.red_team_findings, information_gaps=self.information_gaps,
            most_decision_changing_next_analysis=self.most_decision_changing_next_analysis,
            reasoning=self.reasoning, novelty=self.novelty, stopping_reason=self.stopping_reason,
            synthesis_label=MODE, scientific_validation="SCIENTIFICALLY_UNVALIDATED")

    @model_validator(mode="after")
    def structure(self):
        self.assessment()
        return self


class SourceFamily(FrozenModel):
    interpretation_id: str
    family_id: str
    lineage_keys: tuple[str, ...]
    independence: str


class DecisionProviderSnapshot(FrozenModel):
    schema_version: Literal["llm-decision-snapshot-v1"] = "llm-decision-snapshot-v1"
    decision_input_schema_version: str
    decision_rubric_version: str
    hypothesis: Hypothesis
    project_context: ProjectInterpretationContext
    selected: tuple[SelectedInterpretation, ...]
    factual_context: tuple[EvidenceContext, ...]
    red_team_review: tuple[RedTeamFinding, ...]
    source_families: tuple[SourceFamily, ...]
    coverage_audits: tuple[PublicSearchCoverageAudit, ...] = ()
    grounded_risk_questions: tuple[GroundedRiskQuestion, ...] = ()
    synthesis_contract_version: str | None = None
    synthesis_validation_version: str | None = None
    synthesis_rubric_version: str | None = None
    synthesis_rubric: str = RUBRIC
    candidate_schema_json: str


def provider_snapshot(data: DecisionInput, *, contract_version: str | None = None) -> DecisionProviderSnapshot:
    # Explicit allowlist: no final decision, case expectation, raw records, previous
    # decision output, unrestricted project history, or transport metadata.
    version = contract_version or (data.provider.prompt_version if data.provider.prompt_version in
        SUPPORTED_CONTRACT_VERSIONS else DEFAULT_PROMPT_VERSION)
    if version != DEFAULT_PROMPT_VERSION and (data.coverage_audits or data.grounded_risk_questions or any(
            c.treatment_specificity_tested is not None or c.perturbation_specificity_tested is not None
            for c in data.factual_context)):
        raise ValueError("cannot project prospective v3 input into a historical contract")
    rubric, schema = synthesis_contract(version)
    return DecisionProviderSnapshot(decision_input_schema_version=data.schema_version,
        decision_rubric_version=data.rubric_version, hypothesis=data.hypothesis,
        project_context=data.project_context,
        selected=tuple(SelectedInterpretation.model_validate({**s.model_dump(),
            "execution_provenance_json": None}) for s in data.selected),
        factual_context=tuple(sorted(data.factual_context, key=lambda c:c.interpretation_id)),
        red_team_review=data.red_team_review,
        source_families=tuple(SourceFamily(interpretation_id=i.iid, family_id=i.family_id,
            lineage_keys=i.lineage_keys, independence=i.independence.value) for i in normalize_synthesis(data)),
        coverage_audits=tuple(sorted(data.coverage_audits, key=lambda a: a.audit_id)),
        grounded_risk_questions=data.grounded_risk_questions,
        synthesis_contract_version=version if version == DEFAULT_PROMPT_VERSION else None,
        synthesis_validation_version=VALIDATION_V3 if version == DEFAULT_PROMPT_VERSION else None,
        synthesis_rubric_version=RUBRIC_V3_VERSION if version == DEFAULT_PROMPT_VERSION else None,
        synthesis_rubric=rubric, candidate_schema_json=schema)


def canonical_provider_snapshot(data, *, contract_version=None):
    raw = canonical_json(provider_snapshot(data, contract_version=contract_version).model_dump(mode="json"))
    return raw, sha(raw)


class LLMDecisionProvider:
    """Provider-neutral adapter: future transports implement synthesize(snapshot).

    Real transport admission/authorization is deliberately deferred by the service.
    """
    identity: ProviderIdentity

    def synthesize(self, snapshot: DecisionProviderSnapshot) -> LLMDecisionCandidate:
        raise NotImplementedError


def validate_llm_candidate(data, candidate):
    candidate = LLMDecisionCandidate.model_validate(candidate)
    result = candidate.assessment()
    validate_decision_assessment(data, result)
    selected = {s.interpretation.interpretation_id:s for s in data.selected}
    cited = set()
    for a in result.axes:
        cited.update(a.supporting_interpretation_ids + a.contradictory_interpretation_ids)
    for f in result.red_team_findings:
        cited.update(f.interpretation_ids)
        if f.assessment is RiskAssessment.PRESENT and f.interpretation_ids and not f.source_references:
            raise ValueError("PRESENT findings require source provenance")
        if f.assessment is RiskAssessment.PRESENT and not any(
                r.category == f.category and r.assessment is RiskAssessment.PRESENT
                and set(f.interpretation_ids).issubset(r.interpretation_ids)
                for r in data.red_team_review):
            raise ValueError("PRESENT risk lacks admitted factual basis")
        if (f.assessment is RiskAssessment.UNRESOLVED and (not f.resolution_question or not f.unresolved)
                and not (data.provider.prompt_version == DEFAULT_PROMPT_VERSION and f in data.red_team_review)):
            raise ValueError("unresolved risks require evidence needed to resolve them")
        if f.assessment is RiskAssessment.ABSENT:
            observations = [o for c in data.factual_context if c.interpretation_id in f.interpretation_ids
                            for o in c.risk_observations if o.category == f.category
                            and o.assessment is RiskAssessment.ABSENT]
            if not observations:
                raise ValueError("risk absence must have explicit input evidence")
    for g in result.information_gaps:
        cited.update(g.interpretation_ids)
    if result.most_decision_changing_next_analysis:
        n = result.most_decision_changing_next_analysis
        cited.update(n.interpretation_ids)
        if n.action.lower().strip(" .") in {"collect more data", "review literature", "do more analysis"}:
            raise ValueError("next analysis must be specific")
        gaps = {g.gap_id:g for g in result.information_gaps}
        if not set(n.interpretation_ids).issuperset(i for gid in n.gap_ids for i in gaps[gid].interpretation_ids):
            raise ValueError("next analysis must cite its motivating gap interpretations")
    used = {u.interpretation_id:u for u in candidate.used_interpretations}
    if len(used) != len(candidate.used_interpretations) or set(used) != cited:
        raise ValueError("used interpretation provenance must exactly cover output citations")
    for iid,u in used.items():
        s = selected[iid]
        if (u.evidence_id != s.provenance.evidence_id or u.source_scope != s.provenance.source_scope):
            raise ValueError("used interpretation source identity mismatch")
        allowed = {r.model_dump_json() for r in s.interpretation.rationale_references}
        allowed.update(r.model_dump_json() for c in data.factual_context if c.interpretation_id == iid
                       for r in c.source_references + tuple(r for o in c.risk_observations for r in o.source_references))
        if any(r.model_dump_json() not in allowed for r in u.source_references):
            raise ValueError("unsupported scientific source reference")
    # Detect explicit forbidden numeric claims in prose too. General semantic truth
    # cannot be certified by structural validation and still requires expert review.
    prose = canonical_json(candidate.model_dump(mode="json"))
    if re.search(r'(?:overall.{0,15}(?:gene.{0,5})?score|success.{0,12}probability|wet.lab.{0,12}(?:probability|success))\s*(?:is|of|:|=)?\s*\d', prose, re.I):
        raise ValueError("overall scores and wet-lab probabilities are forbidden")
    admitted = canonical_json(provider_snapshot(data).model_dump(mode="json"))
    for fact in re.findall(r"\b(?:PMID\s*[:=]\s*\d+|p\s*(?:=|<|>)\s*(?:0?\.\d+|1(?:\.0+)?)|p[- ]?value\s*[:=]\s*0?\.\d+)", prose, re.I):
        if fact.lower() not in admitted.lower():
            raise ValueError("unsupported publication/statistical fact")
    if data.provider.prompt_version == DEFAULT_PROMPT_VERSION:
        from genedecision.decision.validation_v3 import validate_v3_candidate
        validate_v3_candidate(data, candidate)
    elif ("information_gap_unknown_rationale" in candidate.model_fields_set
          or any("primary_analysis_kind" in g.model_fields_set for g in candidate.information_gaps)):
        raise ValueError("prospective output fields require decision-synthesis-v3")
    return result
