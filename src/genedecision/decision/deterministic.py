"""Interpretable, scientifically unvalidated qualitative decision baseline v1.

No evidence arithmetic. Gate order and analysis precedence are documented in
DECISION_BASELINE_V1.md. Inputs are selected interpretations and cited facts.
"""

from genedecision.decision.families import (
    normalize_synthesis, distinct_sources, complementary_sources,
)
from genedecision.models.decision import DecisionState
from genedecision.models.decision_core import (
    AnalysisKind, AxisAssessment, AxisStatus, DecisionAssessment, DecisionAxis,
    DecisionTrace, GateCondition, DecisionRelevance, EvidenceRoute, ExpectedImpact,
    InformationGap, InterventionKind, NextAnalysis, NoveltyAssessment, NoveltyStatus,
    ProviderIdentity, RedTeamFinding, RiskAssessment, RiskCategory, SourceKind,
)
from genedecision.models.interpretation import ProviderType


# Ordered qualitative policy, never evidence points. Within a kind: gap_id lexical.
ANALYSIS_PRECEDENCE = (
    AnalysisKind.COMPARATOR_AWARE_ANALYSIS,
    AnalysisKind.DEPENDENCY_CONFOUNDER_ANALYSIS,
    AnalysisKind.TREATMENT_SPECIFIC_ANALYSIS,
    AnalysisKind.ALTERNATIVE_MODEL_CHECK,
    AnalysisKind.CELL_TYPE_DECOMPOSITION,
    AnalysisKind.STRATIFIED_REANALYSIS,
    AnalysisKind.REPLICATE_VALIDATION,
    AnalysisKind.PUBLIC_DATASET_VALIDATION,
    AnalysisKind.LITERATURE_SOURCE_RETRIEVAL,
)


def ids(items):
    return tuple(sorted(i.iid for i in items))


def target_relevant(item):
    c = item.context
    return bool(c and c.outcome_relevant is True and c.model_matches is True
                and c.treatment_phenotype_matches is True)


def direct_path(item):
    return target_relevant(item) and item.judgment.directness.value == "DIRECT"


def causal_path(item):
    return (direct_path(item) and item.judgment.design_type.value == "PERTURBATIONAL"
            and item.context.comparator_identified is True
            and item.context.intervention_kind in (InterventionKind.GENETIC, InterventionKind.FUNCTIONAL))


def citations(items):
    refs = {}
    for item in items:
        for ref in item.judgment.rationale_references + (item.context.source_references if item.context else ()):
            refs[ref.model_dump_json()] = ref
    return tuple(refs[k] for k in sorted(refs))


class DeterministicDecisionProvider:
    identity = ProviderIdentity(provider_type=ProviderType.RULE_BASED,
        provider_name="genedecision-deterministic", provider_version="decision-baseline-v1",
        rule_version="decision-baseline-v1")

    def assess(self, data):
        items = normalize_synthesis(data)
        internal = tuple(i for i in items if i.selected.provenance.source_kind is SourceKind.USER)
        external = tuple(i for i in items if i.selected.provenance.source_kind is not SourceKind.USER)
        support = tuple(i for i in items if i.judgment.support.value == "SUPPORT")
        negative = tuple(i for i in items if i.judgment.support.value == "CONTRADICT")
        meaningful = tuple(i for i in support if direct_path(i))
        causal_support = tuple(i for i in support if causal_path(i))
        causal_negative = tuple(i for i in negative if causal_path(i))
        direct_negative = tuple(i for i in negative if direct_path(i))
        mechanism = tuple(i for i in items if i.judgment.mechanistic_relevance.value == "MECHANISTIC")
        # Public records must have an identifiable origin distinct from every
        # selected internal family before they strengthen independent validation.
        independent = tuple(i for i in external if i in meaningful and i.lineage_keys
                            and ((internal and all(distinct_sources(i, original) for original in internal))
                                 or (not internal and any(distinct_sources(i, other)
                                                         for other in external if other in meaningful))))
        complementary = complementary_sources(meaningful)
        confirmed = bool(independent or complementary)
        findings = list(data.red_team_review)  # Required 5A screen is immutable.

        def finding(category, selected_items, text, severity=DecisionRelevance.MATERIAL,
                    assessment=RiskAssessment.PRESENT, unresolved=True, refs=None):
            findings.append(RedTeamFinding(finding_id="RT_V1_" + category.value,
                category=category, explanation=text, interpretation_ids=ids(selected_items),
                evidence_ids=tuple(sorted({i.selected.provenance.evidence_id for i in selected_items})),
                severity=severity, assessment=assessment, unresolved=unresolved,
                source_references=citations(selected_items) if refs is None else refs))

        if causal_negative:
            finding(RiskCategory.CONTRADICTORY_EVIDENCE, causal_negative,
                    "Direct matched perturbation with identified comparator contradicts the target hypothesis.",
                    DecisionRelevance.BLOCKING)
        if meaningful and direct_negative:
            scope_conflict = any(a.selected.provenance.source_scope != b.selected.provenance.source_scope
                                 for a in meaningful for b in direct_negative)
            finding(RiskCategory.OPPOSITE_DIRECTION, meaningful + direct_negative,
                    "Target-relevant direct support and direct contradiction coexist; resolve direction before investment."
                    + (" USER and PUBLIC scopes disagree." if scope_conflict else ""),
                    DecisionRelevance.BLOCKING)
        associations = tuple(i for i in support if i.judgment.design_type.value in ("OBSERVATIONAL", "ASSOCIATIONAL"))
        observational_negative = tuple(i for i in negative if i.judgment.design_type.value in ("OBSERVATIONAL", "ASSOCIATIONAL"))
        if (associations and causal_negative) or (causal_support and observational_negative):
            finding(RiskCategory.OBSERVATIONAL_CAUSAL_MISMATCH,
                    associations + causal_negative + causal_support + observational_negative,
                    "Perturbational and observational directions disagree; design differences do not silently settle the conflict.")
        duplicates = tuple(i for i in items if any(j.iid != i.iid and j.family_id == i.family_id for j in items))
        if duplicates:
            finding(RiskCategory.DUPLICATED_NONINDEPENDENT_EVIDENCE, duplicates,
                    "Source lineage places these selections in shared families; rows do not add confirmation.",
                    DecisionRelevance.LIMITED)
        if meaningful and not confirmed:
            finding(RiskCategory.WEAK_REPLICATION, meaningful,
                    "Independent confirmation is not established in the selected set; replication outside this set is unassessed.",
                    assessment=RiskAssessment.UNRESOLVED)
        if any(i.selected.provenance.source_kind is SourceKind.DEPMAP for i in items):
            for category in (RiskCategory.COMMON_ESSENTIAL_BASELINE_FITNESS, RiskCategory.TREATMENT_INDEPENDENT_DEPENDENCY):
                unassessed_items = tuple(i for i in items if i.selected.provenance.source_kind is SourceKind.DEPMAP
                    and not (i.context and any(c.category is category and c.assessment is not RiskAssessment.UNASSESSED
                                              for c in i.context.risk_observations)))
                if unassessed_items:
                    finding(category, unassessed_items,
                            "Dependency-source context makes this check relevant, but no cited confounder assessment is supplied.",
                            assessment=RiskAssessment.UNRESOLVED)
        # Explicit absent observations are evidence of absence only for the
        # cited items. Never derive absence from a missing category or search hit.
        # screen_red_team preserves supplied observations and exact references.
        remediable = tuple(f for f in findings if f.unresolved
                           and f.assessment in (RiskAssessment.PRESENT, RiskAssessment.UNRESOLVED)
                           and f.severity in (DecisionRelevance.BLOCKING, DecisionRelevance.MATERIAL)
                           and f.category in (
                               RiskCategory.COMMON_ESSENTIAL_BASELINE_FITNESS,
                               RiskCategory.NONSPECIFIC_PERTURBATION,
                               RiskCategory.TREATMENT_INDEPENDENT_DEPENDENCY,
                               RiskCategory.DISEASE_MODEL_MISMATCH,
                               RiskCategory.TREATMENT_PHENOTYPE_MISMATCH,
                               RiskCategory.CELL_COMPOSITION, RiskCategory.INSUFFICIENT_COMPARATOR))
        incomplete_negative = tuple(i for i in negative if i.judgment.directness.value != "CONTEXT_ONLY" and not causal_path(i))
        unsettled_target = tuple(i for i in items if i.judgment.directness.value != "CONTEXT_ONLY"
            and ((i.judgment.support.value == "SUPPORT" and i.judgment.directness.value == "DIRECT" and not direct_path(i))
                 or (i.judgment.support.value == "UNKNOWN" and
                     (i.judgment.directness.value == "DIRECT" or i.judgment.design_type.value == "PERTURBATIONAL"))))
        explicit_replication = tuple(f for f in findings if f.category is RiskCategory.WEAK_REPLICATION
            and f.unresolved and f.assessment is RiskAssessment.PRESENT
            and f.severity in (DecisionRelevance.BLOCKING, DecisionRelevance.MATERIAL))
        dominant_negative = bool(causal_negative and not meaningful and not incomplete_negative
                                 and not unsettled_target and not remediable and not explicit_replication)
        gaps = []
        actions = {}

        def gap(code, kind, question, action, selected_items, priority=DecisionRelevance.MATERIAL,
                impact=ExpectedImpact.COULD_STRENGTHEN, route=EvidenceRoute.COMPUTATIONAL_PUBLIC):
            value = InformationGap(gap_id="GAP_" + code, missing_question=question,
                why_it_matters="Resolving this selected-set limitation could change the decision gate; no outcome is assumed.",
                evidence_type_needed=kind.value, route=route, expected_decision_impact=impact,
                priority=priority, actionable=route is EvidenceRoute.COMPUTATIONAL_PUBLIC,
                interpretation_ids=ids(selected_items))
            gaps.append(value)
            actions[value.gap_id] = (kind, action)

        if not dominant_negative:
            conflict = bool(negative and (meaningful or unsettled_target or not causal_negative))
            if conflict:
                gap("conflicting_direction", AnalysisKind.COMPARATOR_AWARE_ANALYSIS,
                    "Do matched comparator/model definitions resolve the conflicting directional interpretations?",
                    "Retrieve the cited comparator and cohort definitions, harmonize treatment/model strata, and re-analyze the selected opposing outcomes.",
                    meaningful + unsettled_target + negative, DecisionRelevance.BLOCKING, ExpectedImpact.COULD_RESOLVE_CONTRADICTION)
            candidate = tuple(i for i in items if i.judgment.directness.value != "CONTEXT_ONLY"
                              and i.judgment.support.value != "NEUTRAL")
            comparator_missing = tuple(i for i in candidate if not i.context or i.context.comparator_identified is not True)
            if comparator_missing:
                gap("comparator", AnalysisKind.COMPARATOR_AWARE_ANALYSIS,
                    "Is the target outcome supported by an identifiable comparison?",
                    "Retrieve comparator definitions for the cited assays, reconstruct treatment-versus-control contrasts, and submit a separate interpretation of the matched outcome.",
                    comparator_missing, DecisionRelevance.BLOCKING)
            for code, field, kind, action in (
                ("outcome", "outcome_relevant", AnalysisKind.STRATIFIED_REANALYSIS,
                 "Retrieve phenotype definitions and re-analyze the cited datasets for the confirmed target outcome rather than a surrogate measurement."),
                ("model", "model_matches", AnalysisKind.ALTERNATIVE_MODEL_CHECK,
                 "Retrieve model/cohort annotations and validate the selected direction in a disease-matched public cohort or dependency profile."),
                ("treatment", "treatment_phenotype_matches", AnalysisKind.TREATMENT_SPECIFIC_ANALYSIS,
                 "Retrieve treatment annotations, stratify treated and untreated samples, and test whether the selected effect bears on the target treatment phenotype."),
            ):
                missing = tuple(i for i in candidate if not i.context or getattr(i.context, field) is not True)
                if missing:
                    gap(code, kind, "Does the cited evidence match the target " + code + " context?", action, missing)
            for category, kind, action in (
                (RiskCategory.INSUFFICIENT_COMPARATOR, AnalysisKind.COMPARATOR_AWARE_ANALYSIS,
                 "Retrieve matched comparator definitions and re-analyze the cited effect under the stated target contrast."),
                (RiskCategory.COMMON_ESSENTIAL_BASELINE_FITNESS, AnalysisKind.DEPENDENCY_CONFOUNDER_ANALYSIS,
                 "Retrieve release-matched common-essential annotations and compare target-lineage dependency with baseline fitness controls in the cited profile."),
                (RiskCategory.NONSPECIFIC_PERTURBATION, AnalysisKind.DEPENDENCY_CONFOUNDER_ANALYSIS,
                 "Re-analyze cited perturbation controls and public orthogonal interventions to test whether nonspecific fitness explains the target effect."),
                (RiskCategory.TREATMENT_INDEPENDENT_DEPENDENCY, AnalysisKind.TREATMENT_SPECIFIC_ANALYSIS,
                 "Stratify the cited dependency profile by available treatment/control annotations and test treatment specificity against baseline dependency."),
                (RiskCategory.CELL_COMPOSITION, AnalysisKind.CELL_TYPE_DECOMPOSITION,
                 "Use cited cell annotations to decompose composition and within-cell expression, then re-interpret the target effect after adjustment."),
            ):
                risks = tuple(f for f in remediable if f.category is category)
                if risks:
                    referenced = {iid for f in risks for iid in f.interpretation_ids}
                    gap(category.value.lower(), kind, "Can the cited " + category.value.lower() + " limitation explain the target effect?",
                        action, tuple(i for i in items if i.iid in referenced), DecisionRelevance.BLOCKING,
                        ExpectedImpact.COULD_WEAKEN)
            if (meaningful and not confirmed) or explicit_replication:
                gap("independent_validation", AnalysisKind.REPLICATE_VALIDATION,
                    "Does an independent matched source reproduce the target direction?",
                    f"Identify an independent public cohort or dataset for {data.gene} in {data.project_context.disease} under {data.project_context.treatment}; verify lineage and compare the target phenotype with matched controls.",
                    meaningful or causal_negative or items, DecisionRelevance.MATERIAL)
            if not causal_support:
                gap("direct_test", AnalysisKind.LITERATURE_SOURCE_RETRIEVAL,
                    "Is a direct hypothesis-relevant controlled perturbation result available?",
                    f"Retrieve primary perturbation literature and available functional datasets for {data.gene}, {data.project_context.disease}, {data.project_context.treatment} and {data.project_context.phenotype}; obtain a separately persisted interpretation of the exact comparison.",
                    items)
            if not items:
                gap("target_evidence", AnalysisKind.PUBLIC_DATASET_VALIDATION,
                    "Can a matched public dataset test the confirmed hypothesis?",
                    f"Locate a public cohort with {data.gene}, {data.project_context.disease}, {data.project_context.treatment} and the target phenotype; inspect comparison availability before deriving a new interpretation.", items)
        exact_prior = tuple(i for i in external if direct_path(i)
                            and i.selected.provenance.source_kind is SourceKind.PUBMED
                            and any(k.startswith("PMID:") for k in i.lineage_keys))
        novelty = NoveltyAssessment(status=NoveltyStatus.PARTIALLY_ESTABLISHED if exact_prior else NoveltyStatus.UNKNOWN,
            reasoning="Explicit matched direct prior publication evidence exists, without proof that the full hypothesis is established."
            if exact_prior else "Selected retrievals lack a coverage audit; absence of publications does not establish novelty.")
        # Coverage uncertainty is recorded but cannot decide readiness alone.
        if not exact_prior:
            gap("search_coverage", AnalysisKind.LITERATURE_SOURCE_RETRIEVAL,
                "Is exact-hypothesis publication search coverage documented?",
                "Audit query dimensions and source availability for the confirmed hypothesis; retain retrieval provenance without treating zero hits as novelty.",
                external, DecisionRelevance.LIMITED, ExpectedImpact.UNKNOWN)
        if meaningful and not mechanism:
            gap("mechanistic_context", AnalysisKind.LITERATURE_SOURCE_RETRIEVAL,
                "Is a source-grounded mechanistic explanation available?",
                "Retrieve primary mechanistic studies for the selected target relationship and obtain contextual interpretations distinct from causal validation.",
                meaningful, DecisionRelevance.LIMITED)
        changing = tuple(g for g in gaps if g.actionable and g.priority in (DecisionRelevance.BLOCKING, DecisionRelevance.MATERIAL))
        blockers = tuple(f for f in findings if f.unresolved and f.severity is DecisionRelevance.BLOCKING
                         and f.assessment in (RiskAssessment.PRESENT, RiskAssessment.UNRESOLVED))
        if dominant_negative:
            state = DecisionState.DEPRIORITIZE
            gate = "DIRECT_CONTRADICTION_DOMINATES"
            reasoning = "Matched controlled perturbation contradicts the hypothesis; support has no meaningful direct path and no cited remediable conflict remains."
            stopping = "Further pre-wet-lab computation is unlikely to rescue the selected hypothesis under this baseline gate: qualified direct contradiction remains and other support is contextual/indirect. This is scientifically unvalidated."
        elif causal_support and meaningful and not changing and not blockers:
            state = DecisionState.PRIORITIZE
            gate = "GROUNDED_SUPPORT_WITHOUT_DECISION_CHANGING_GAP"
            reasoning = "Target-relevant direct causal support has distinct-source confirmation or complementary modalities; no blocking contradiction or material computational gap remains."
            stopping = None
        else:
            state = DecisionState.COMPUTE_MORE
            gate = "RESOLVE_MATERIAL_GAP_OR_CONFLICT"
            reasoning = "The selected set is incomplete or conflicting; a specific computational/public step could change the gate."
            stopping = None
            if not changing:
                gap("blocking_review", AnalysisKind.STRATIFIED_REANALYSIS,
                    "Can source-stratified review resolve the cited blocking limitation?",
                    "Retrieve the cited blocking source definitions, compare matched strata and controls, and record a separately grounded resolution of the risk.",
                    items, DecisionRelevance.BLOCKING, ExpectedImpact.COULD_RESOLVE_CONTRADICTION)
                changing = tuple(g for g in gaps if g.priority is DecisionRelevance.BLOCKING)
        next_analysis = None
        next_reason = "No material computational/public gap remains under the selected gate; limited coverage/context gaps cannot independently block investment."
        if state is DecisionState.COMPUTE_MORE:
            ordered = tuple(g for relevance in (DecisionRelevance.BLOCKING, DecisionRelevance.MATERIAL)
                for kind in ANALYSIS_PRECEDENCE for g in sorted(changing, key=lambda g: g.gap_id)
                if g.priority is relevance and actions[g.gap_id][0] is kind)
            chosen = ordered[0]
            kind, action = actions[chosen.gap_id]
            next_reason = ("Select blocking before material gaps; then comparator, dependency confounder, treatment, model, cell composition, stratification, replicate, public dataset, literature. "
                           "Within a kind use lexical gap ID. Chosen: " + chosen.gap_id + "; alternatives: " + ", ".join(g.gap_id for g in ordered[1:]) + ".")
            next_analysis = NextAnalysis(kind=kind, action=action, selection_reason=next_reason,
                gap_ids=(chosen.gap_id,), interpretation_ids=chosen.interpretation_ids)

        def evidence_axis(axis, scoped, independent_axis=False):
            pos = tuple(i for i in scoped if i.judgment.support.value == "SUPPORT")
            neg = tuple(i for i in scoped if i.judgment.support.value == "CONTRADICT")
            direct = tuple(i for i in pos if direct_path(i))
            if not scoped:
                status = AxisStatus.ABSENT
            elif direct and any(direct_path(i) for i in neg):
                status = AxisStatus.MIXED
            elif pos and neg:
                status = AxisStatus.MIXED
            elif direct and not neg and ((independent_axis and any(i in independent for i in direct)) or
                    (not independent_axis and complementary_sources(direct) and any(causal_path(i) for i in direct))) and not remediable:
                status = AxisStatus.STRONG
            elif direct or any(causal_path(i) for i in neg):
                status = AxisStatus.MODERATE
            elif pos or neg:
                status = AxisStatus.WEAK
            else:
                status = AxisStatus.UNKNOWN
            text = ("Direct matched public evidence strengthens independent validation only with identifiable origins distinct from the internal set; contextual literature is not validation."
                    if independent_axis else "Direct perturbation and direct observation remain distinct; complementary cited modalities from distinct families can establish a strong coherent internal path.")
            details = tuple(i.iid + ": " + i.judgment.support.value + "/" + i.judgment.directness.value + "/" + i.judgment.design_type.value
                            + "; family=" + i.family_id + "; lineage=" + i.independence.value
                            + "; modality=" + (i.context.modality if i.context and i.context.modality else "UNKNOWN") for i in scoped)
            return AxisAssessment(axis=axis, status=status, reasoning=text + " " + " | ".join(details),
                supporting_interpretation_ids=ids(pos), contradictory_interpretation_ids=ids(neg))

        causal_status = AxisStatus.MIXED if causal_support and direct_negative else (
            AxisStatus.STRONG if any(i.context.intervention_kind is InterventionKind.GENETIC for i in causal_support) and not remediable and not negative else
            AxisStatus.MODERATE if causal_support or causal_negative else
            AxisStatus.WEAK if meaningful else AxisStatus.ABSENT if not items else AxisStatus.UNKNOWN)
        active = tuple(f for f in findings if f.unresolved and f.assessment in (RiskAssessment.PRESENT, RiskAssessment.UNRESOLVED))
        risk_status = AxisStatus.MIXED if meaningful and negative else AxisStatus.STRONG if causal_negative or blockers else AxisStatus.MODERATE if remediable else AxisStatus.WEAK if negative else AxisStatus.UNKNOWN
        axes = (
            evidence_axis(DecisionAxis.INTERNAL_EVIDENCE, internal),
            evidence_axis(DecisionAxis.INDEPENDENT_EVIDENCE, external, True),
            AxisAssessment(axis=DecisionAxis.CAUSAL_EVIDENCE, status=causal_status,
                reasoning="Genetic controlled perturbation precedes functional perturbation; longitudinal response and association remain weaker paths. DIRECT/design/SUPPORT alone are insufficient; comparator, relevant outcome and matched context must jointly qualify.",
                supporting_interpretation_ids=ids(causal_support or meaningful), contradictory_interpretation_ids=ids(causal_negative or direct_negative)),
            AxisAssessment(axis=DecisionAxis.BIOLOGICAL_PLAUSIBILITY, status=AxisStatus.MODERATE if mechanism else AxisStatus.ABSENT if not items else AxisStatus.UNKNOWN,
                reasoning="Examined mechanisms provide contextual plausibility, including negative mechanism investigations; this axis cannot independently trigger PRIORITIZE.", supporting_interpretation_ids=ids(mechanism)),
            AxisAssessment(axis=DecisionAxis.NOVELTY, status=AxisStatus.MODERATE if exact_prior else AxisStatus.UNKNOWN,
                reasoning=novelty.reasoning, supporting_interpretation_ids=ids(exact_prior)),
            AxisAssessment(axis=DecisionAxis.CONTRADICTIONS_RISK, status=risk_status,
                reasoning="Contradictory judgments and cited risks remain explicit. Unassessed categories do not imply absence; duplicate families add no confirmation.",
                contradictory_interpretation_ids=ids(negative), unresolved_risks=tuple(f.finding_id for f in active)),
            AxisAssessment(axis=DecisionAxis.INFORMATION_GAP, status=AxisStatus.STRONG if any(g.priority is DecisionRelevance.BLOCKING for g in gaps) else AxisStatus.MODERATE if changing else AxisStatus.WEAK if gaps else AxisStatus.ABSENT,
                reasoning="Only material computational/public gaps control the next-analysis gate; limited novelty or mechanistic uncertainty cannot decide readiness alone.",
                missing_information=tuple(g.missing_question for g in gaps), unresolved_risks=tuple(f.finding_id for f in remediable)),
        )
        trace = DecisionTrace(gate=gate, conditions=(
            GateCondition(condition="Grounded direct/causal paths and source-family confirmation, excluding contextual-only support.",
                axes=(DecisionAxis.INTERNAL_EVIDENCE, DecisionAxis.INDEPENDENT_EVIDENCE, DecisionAxis.CAUSAL_EVIDENCE),
                interpretation_ids=ids(meaningful + causal_negative),
                finding_ids=tuple(f.finding_id for f in active if f.category is RiskCategory.DUPLICATED_NONINDEPENDENT_EVIDENCE)),
            GateCondition(condition="Contradiction dominance or unresolved blocking conflict is explicit.",
                axes=(DecisionAxis.CONTRADICTIONS_RISK,), interpretation_ids=ids(negative),
                finding_ids=tuple(f.finding_id for f in active if f.category in (RiskCategory.CONTRADICTORY_EVIDENCE, RiskCategory.OPPOSITE_DIRECTION) or f in remediable or f in blockers)),
            GateCondition(condition="Material gaps require computation; novelty and plausibility alone do not decide.",
                axes=(DecisionAxis.INFORMATION_GAP, DecisionAxis.NOVELTY, DecisionAxis.BIOLOGICAL_PLAUSIBILITY),
                interpretation_ids=tuple(sorted({i for g in changing for i in g.interpretation_ids})),
                finding_ids=tuple(f.finding_id for f in active if f.category in (
                    RiskCategory.WEAK_REPLICATION, RiskCategory.MISSING_DIRECT_CAUSAL_TEST)),
                gap_ids=tuple(g.gap_id for g in changing)),
        ), next_analysis_reason=next_reason, considered_gap_ids=tuple(g.gap_id for g in gaps))
        return DecisionAssessment(decision=state, reasoning=reasoning, axes=axes,
            red_team_findings=tuple(findings), information_gaps=tuple(gaps),
            most_decision_changing_next_analysis=next_analysis, novelty=novelty,
            stopping_reason=stopping, trace=trace, synthesis_label="DETERMINISTIC_BASELINE",
            scientific_validation="SCIENTIFICALLY_UNVALIDATED")
