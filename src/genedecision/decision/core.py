"""Provider-neutral decision lifecycle and a deliberately conservative baseline."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Protocol, TYPE_CHECKING
from uuid import uuid4

from genedecision.decision.red_team import screen_red_team
from genedecision.interpretation.core import canonical_input_snapshot
from genedecision.models.decision import DecisionState
from genedecision.models.decision_core import (
    AnalysisKind, AxisAssessment, AxisStatus, DecisionAssessment, DecisionAxis,
    DecisionInput, DecisionRelevance, DecisionReport, DecisionRun, DecisionRunStatus,
    EvidenceContext, EvidenceProvenance, EvidenceRoute, ExpectedImpact, InformationGap,
    NextAnalysis, NoveltyAssessment, ProviderIdentity, RiskAssessment, RiskCategory,
    SelectedInterpretation, SourceIdentity, SourceKind, canonical_decision_snapshot,
    validate_decision_assessment, PublicSearchCoverageAudit, GroundedRiskQuestion,
)
from genedecision.models.evidence import SourceScope
from genedecision.models.interpretation import (
    InterpretationRequest, InterpretationStatus, ProjectInterpretationContext,
    ProviderType, validate_rationale_references,
)
from genedecision.storage import EvidenceDatabase


if TYPE_CHECKING:
    from genedecision.decision.llm import LLMDecisionProvider


class DecisionProvider(Protocol):
    identity: ProviderIdentity

    def assess(self, decision_input: DecisionInput) -> DecisionAssessment: ...


class MockDecisionProvider:
    """Explicit fixtures only; never a validated scientific recommendation."""
    identity = ProviderIdentity(provider_type=ProviderType.MOCK,
                                provider_name="decision-fixture", provider_version="1")

    def __init__(self, assessment):
        self.assessment = assessment

    def assess(self, decision_input):
        return self.assessment(decision_input) if callable(self.assessment) else self.assessment


class ConservativeDecisionProvider:
    """v0 gap-finding baseline, always COMPUTE_MORE. No biological ranking rules."""
    identity = ProviderIdentity(provider_type=ProviderType.RULE_BASED,
        provider_name="conservative-decision-baseline", provider_version="1",
        rule_version="decision-gap-baseline-v0")

    def assess(self, data):
        ids = tuple(s.interpretation.interpretation_id for s in data.selected)
        negative = tuple(s.interpretation.interpretation_id for s in data.selected
                         if s.interpretation.support.value == "CONTRADICT")
        contexts = {c.interpretation_id: c for c in data.factual_context}
        active = [f for f in data.red_team_review if f.assessment is RiskAssessment.PRESENT and f.unresolved]
        active = [f for relevance in (DecisionRelevance.BLOCKING, DecisionRelevance.MATERIAL,
                  DecisionRelevance.LIMITED, DecisionRelevance.UNASSESSED)
                  for f in active if f.severity is relevance]
        confounders = [f for f in active if f.category in (
            RiskCategory.COMMON_ESSENTIAL_BASELINE_FITNESS, RiskCategory.NONSPECIFIC_PERTURBATION,
            RiskCategory.TREATMENT_INDEPENDENT_DEPENDENCY, RiskCategory.CELL_COMPOSITION)]
        if confounders:
            kind = AnalysisKind.CELL_TYPE_DECOMPOSITION if confounders[0].category is RiskCategory.CELL_COMPOSITION else AnalysisKind.DEPENDENCY_CONFOUNDER_ANALYSIS
            question = "Does the cited alternative explanation account for the apparent target effect?"
            action = "Retrieve control definitions or public dataset availability for the cited analysis, then test treatment/control stratification and the reported confounder against its original phenotype comparison."
            impact = ExpectedImpact.COULD_WEAKEN
        elif negative:
            kind = AnalysisKind.COMPARATOR_AWARE_ANALYSIS
            question = "Can harmonized comparator and context analysis resolve the selected opposite-direction judgments?"
            action = "Reconstruct comparator, treatment and model definitions for the cited contradictory items; re-analyze their direction under matched strata."
            impact = ExpectedImpact.COULD_RESOLVE_CONTRADICTION
        elif data.selected and any(i not in contexts or contexts[i].comparator_identified is not True for i in ids):
            kind = AnalysisKind.COMPARATOR_AWARE_ANALYSIS
            question = "Is the direction grounded in an identifiable treatment-versus-control outcome comparison?"
            action = "Retrieve comparator definitions for the selected assays and re-analyze the target outcome by treatment and control before interpreting the result again."
            impact = ExpectedImpact.UNKNOWN
        else:
            kind = AnalysisKind.PUBLIC_DATASET_VALIDATION if data.selected else AnalysisKind.LITERATURE_SOURCE_RETRIEVAL
            question = "Can an independent matched public source test the confirmed gene-treatment-phenotype relationship?"
            context = data.project_context
            action = f"Search public source records for {data.gene} in {context.disease} under {context.treatment} with {context.phenotype}; check dataset availability and obtain a separate matched interpretation."
            impact = ExpectedImpact.COULD_STRENGTHEN
        gap = InformationGap(gap_id="GAP_primary", missing_question=question,
            why_it_matters="This result could change whether the evidence warrants wet-lab investment; no future outcome is assumed.",
            evidence_type_needed=kind.value, route=EvidenceRoute.COMPUTATIONAL_PUBLIC,
            expected_decision_impact=impact, priority=DecisionRelevance.MATERIAL,
            actionable=True, interpretation_ids=ids)
        axes = []
        for axis in DecisionAxis:
            supporting = ()
            contradictory = ()
            missing = ()
            status = AxisStatus.UNKNOWN
            reasoning = "The v0 baseline does not establish a scientific strength judgment for this axis."
            if axis in (DecisionAxis.INTERNAL_EVIDENCE, DecisionAxis.INDEPENDENT_EVIDENCE):
                scope = SourceScope.USER if axis is DecisionAxis.INTERNAL_EVIDENCE else SourceScope.PUBLIC
                scoped = [s for s in data.selected if s.provenance.source_scope is scope]
                supporting = tuple(s.interpretation.interpretation_id for s in scoped
                                   if s.interpretation.support.value == "SUPPORT" and s.interpretation.directness.value != "CONTEXT_ONLY")
                contradictory = tuple(s.interpretation.interpretation_id for s in scoped if s.interpretation.support.value == "CONTRADICT")
                status = AxisStatus.ABSENT if not scoped else AxisStatus.MIXED if supporting and contradictory else AxisStatus.WEAK if supporting or contradictory else AxisStatus.UNKNOWN
                reasoning = "Selected judgments are listed by source scope; repeated modalities or publications are not treated as independent confirmations. Strength and independence are not certified."
                missing = ("Independent replication and dataset/modality lineage remain required context.",)
            elif axis is DecisionAxis.CAUSAL_EVIDENCE:
                missing = ("Cited intervention, comparator, relevant outcome, context and confounder review are needed; DIRECT alone is not causal.",)
            elif axis is DecisionAxis.BIOLOGICAL_PLAUSIBILITY:
                supporting = tuple(s.interpretation.interpretation_id for s in data.selected if s.interpretation.mechanistic_relevance.value == "MECHANISTIC")
                status = AxisStatus.WEAK if supporting else AxisStatus.UNKNOWN
                reasoning = "Mechanism investigations are contextual plausibility evidence, including negative tests; they do not alone establish directional support or readiness."
            elif axis is DecisionAxis.NOVELTY:
                reasoning = "Selected records and limited search retrieval do not establish public-search coverage or novelty."
                missing = ("A versioned public-search coverage audit is required.",)
            elif axis is DecisionAxis.CONTRADICTIONS_RISK:
                contradictory = negative
                status = AxisStatus.MIXED if negative else AxisStatus.UNKNOWN
                reasoning = "Known directional conflicts and structured Red Team screens remain explicit; unassessed confounders are not asserted absent."
            elif axis is DecisionAxis.INFORMATION_GAP:
                status = AxisStatus.MODERATE
                reasoning = question
                missing = (gap.missing_question,)
            axes.append(AxisAssessment(axis=axis, status=status, reasoning=reasoning,
                supporting_interpretation_ids=supporting, contradictory_interpretation_ids=contradictory,
                missing_information=missing, unresolved_risks=tuple(f.finding_id for f in active) if axis in (
                    DecisionAxis.CONTRADICTIONS_RISK, DecisionAxis.INFORMATION_GAP) else ()))
        return DecisionAssessment(decision=DecisionState.COMPUTE_MORE,
            reasoning="The conservative architecture baseline identifies a pre-wet-lab evidence gap; it does not adjudicate scientific readiness or competitive priority.",
            axes=tuple(axes), red_team_findings=data.red_team_review, information_gaps=(gap,),
            most_decision_changing_next_analysis=NextAnalysis(kind=kind, action=action,
                selection_reason=question, gap_ids=(gap.gap_id,), interpretation_ids=ids),
            novelty=NoveltyAssessment(reasoning="No adequate public-search coverage audit is supplied."))


class DecisionExecutionError(RuntimeError):
    def __init__(self, run_id, reason):
        self.run_id = run_id
        super().__init__(f"decision run {run_id} failed: {reason}")


class DecisionService:
    def __init__(self, database: EvidenceDatabase):
        self.database = database

    def prepare(self, *, project_name: str, gene: str, hypothesis_id: str,
                interpretation_ids, provider: DecisionProvider | LLMDecisionProvider,
                factual_context: tuple[EvidenceContext, ...] = (),
                coverage_audits: tuple[PublicSearchCoverageAudit, ...] = (),
                grounded_risk_questions: tuple[GroundedRiskQuestion, ...] = ()) -> DecisionInput:
        identity = ProviderIdentity.model_validate(provider.identity)
        if not identity.provider_version:
            raise ValueError("decision provider must declare its version")
        manual_identity = identity in tuple(ProviderIdentity(provider_type=ProviderType.LLM,
            provider_name="Codex", provider_version="manual-decision-bridge-v1", prompt_version=v)
            for v in ("decision-synthesis-v1", "decision-synthesis-v2", "decision-synthesis-v3"))
        if identity.provider_type not in (ProviderType.MOCK, ProviderType.RULE_BASED, ProviderType.HUMAN) and not manual_identity:
            raise ValueError("supports local decision providers only; real transport is deferred")
        ids = tuple(interpretation_ids)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate interpretation selection")
        project = self.database.get_project(project_name)
        hypothesis = self.database.get_hypothesis(hypothesis_id)
        if project is None or gene not in project.candidate_genes or hypothesis is None or hypothesis.project_name != project_name:
            raise ValueError("decision requires a persisted project and confirmed hypothesis")
        project_context = ProjectInterpretationContext(project_name=project_name, gene=gene,
            disease=project.disease, phenotype=project.phenotype, treatment=project.drug_or_treatment,
            organism=project.organism, tissue=project.tissue, research_question=project.research_question)
        selected = []
        requests = {}
        for iid in sorted(ids):
            interpretation = self.database.get_interpretation(iid)
            if interpretation is None:
                raise ValueError("selected interpretation does not exist")
            run = self.database.get_interpretation_run(interpretation.run_id)
            if run is None or run.status is not InterpretationStatus.SUCCEEDED:
                raise ValueError("selected interpretation requires a successful run")
            request = InterpretationRequest.model_validate_json(run.input_snapshot)
            _, digest = canonical_input_snapshot(request)
            if digest != run.input_snapshot_hash or request.hypothesis != hypothesis or request.project_context != project_context:
                raise ValueError("selected interpretation snapshot context differs from decision")
            if interpretation.project_name != project_name or interpretation.hypothesis_id != hypothesis_id or interpretation.evidence_id != run.evidence_id or request.evidence_record.gene != gene:
                raise ValueError("selected interpretation project/hypothesis/gene mismatch")
            validate_rationale_references(request, interpretation.rationale_references)
            requests[iid] = request
            execution_metadata = (self.database.get_provider_execution_metadata(run.run_id)
                                  or self.database.get_manual_bridge_metadata(run.run_id))
            evidence = request.evidence_record  # identifiers only; no scientific content classification
            source_kind = SourceKind.USER if evidence.source_scope is SourceScope.USER else SourceKind.PUBMED if evidence.source_name == "PubMed" else SourceKind.DEPMAP if evidence.source_name == "DepMap" else SourceKind.OTHER_PUBLIC
            origins = []
            for record in request.source_metadata.records:
                if record.record_type == "PublicationMetadata":
                    if record.fields.get("pmid"):
                        origins.append("PMID:" + str(record.fields["pmid"]))
                    if record.fields.get("doi"):
                        origins.append("DOI:" + str(record.fields["doi"]).lower())
                if record.record_type == "DepMapReleaseMetadata":
                    origins.append("DEPMAP_RELEASE:" + record.record_id)
            if source_kind is SourceKind.PUBMED and evidence.source_identifier:
                origins.append("PMID:" + evidence.source_identifier)
            selected.append(SelectedInterpretation(interpretation=interpretation, run_id=run.run_id,
                provider=ProviderIdentity(provider_type=run.provider_type, provider_name=run.provider_name,
                    provider_version=run.provider_version, model_version=run.model_version,
                    prompt_version=run.prompt_version, rule_version=run.rule_version, reviewer_id=run.reviewer_id),
                interpretation_snapshot_hash=run.input_snapshot_hash,
                execution_provenance_json=execution_metadata.model_dump_json() if execution_metadata else None,
                provenance=EvidenceProvenance(evidence_id=evidence.evidence_id,
                    source_scope=evidence.source_scope, source_kind=source_kind,
                    source_name=evidence.source_name, source_type=evidence.source_type,
                    source_identifier=evidence.source_identifier,
                    source_records=tuple(sorted((SourceIdentity(record_type=r.record_type, record_id=r.record_id)
                        for r in request.source_metadata.records), key=lambda r:(r.record_type,r.record_id))),
                    origin_keys=tuple(sorted(set(origins))))))
        contexts = tuple(EvidenceContext.model_validate(c) for c in factual_context)
        if len({c.interpretation_id for c in contexts}) != len(contexts):
            raise ValueError("duplicate factual context")
        for context in contexts:
            if context.interpretation_id not in requests:
                raise ValueError("factual context cites an unselected interpretation")
            refs = context.source_references + tuple(r for o in context.risk_observations for r in o.source_references)
            if refs:
                validate_rationale_references(requests[context.interpretation_id], refs)
        contexts = tuple(sorted(contexts, key=lambda c:c.interpretation_id))
        return DecisionInput(project_name=project_name, gene=gene, project_context=project_context,
            hypothesis=hypothesis, selected=tuple(selected), factual_context=contexts,
            red_team_review=screen_red_team(selected, contexts), provider=identity,
            coverage_audits=coverage_audits, grounded_risk_questions=grounded_risk_questions)

    def decide(self, *, provider: DecisionProvider | LLMDecisionProvider, run_id: str | None = None, **selection):
        data = self.prepare(provider=provider, **selection)
        snapshot, digest = canonical_decision_snapshot(data)
        execution = None
        if data.provider.provider_type is ProviderType.LLM:
            from genedecision.decision.manual_bridge import CodexManualDecisionProvider
            from genedecision.decision.llm import provider_snapshot
            if not isinstance(provider, CodexManualDecisionProvider):
                raise ValueError("only the local manual decision transport is enabled")
            execution = provider.execution_provenance(provider_snapshot(data))
        started = datetime.now(timezone.utc)
        run = DecisionRun(run_id=run_id or "DRUN_"+uuid4().hex, project_name=data.project_name,
            hypothesis_id=data.hypothesis.hypothesis_id, provider=data.provider,
            input_snapshot=snapshot, input_snapshot_hash=digest, started_at=started,
            status=DecisionRunStatus.STARTED, execution_provenance=execution)
        self.database.start_decision_run(run)
        clock = time.monotonic()
        report = None
        candidate_json = None
        try:
            # Round-trip creates a detached input; providers receive no database handle or raw text.
            detached = DecisionInput.model_validate_json(snapshot)
            if data.provider.provider_type is ProviderType.LLM:
                from genedecision.decision.llm import provider_snapshot, validate_llm_candidate
                from genedecision.decision.llm import LLMDecisionCandidate, canonical_json
                candidate = LLMDecisionCandidate.model_validate(provider.synthesize(provider_snapshot(detached)))
                result = validate_llm_candidate(data, candidate)
                candidate_json = canonical_json(candidate.model_dump(mode="json"))
                if provider.execution_provenance(provider_snapshot(detached)) != execution:
                    raise ValueError("manual artifacts changed during execution")
            else:
                result = DecisionAssessment.model_validate(provider.assess(detached))
            validate_decision_assessment(data, result)
            if ProviderIdentity.model_validate(provider.identity) != data.provider:
                raise ValueError("provider identity changed during decision")
            finished = datetime.now(timezone.utc)
            report = DecisionReport(report_id="DREPORT_"+uuid4().hex, run_id=run.run_id,
                project_name=data.project_name, hypothesis_id=data.hypothesis.hypothesis_id,
                input_snapshot_hash=digest,
                selected_interpretation_ids=tuple(s.interpretation.interpretation_id for s in data.selected),
                assessment=result, provider_candidate_json=candidate_json, created_at=finished)
            terminal = DecisionRun.model_validate({**run.model_dump(), "status":DecisionRunStatus.SUCCEEDED,
                "completed_at":finished, "latency_ms":int((time.monotonic()-clock)*1000)})
        except Exception:
            report = None
            terminal = DecisionRun.model_validate({**run.model_dump(), "status":DecisionRunStatus.FAILED,
                "completed_at":datetime.now(timezone.utc), "latency_ms":int((time.monotonic()-clock)*1000),
                "error":"decision provider failed or output invalid"})
        self.database.finish_decision_run(terminal, report)
        if report is None:
            raise DecisionExecutionError(run.run_id, terminal.error)
        return terminal, report
