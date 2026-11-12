"""Prospective output consistency only; the original final-state gates are unchanged."""
from genedecision.models.decision import DecisionState
from genedecision.models.decision_core import (
    AxisStatus, DecisionAxis, DecisionRelevance, EvidenceRoute, GroundedRiskQuestion,
    InterventionKind, NoveltyStatus, RiskAssessment, RiskCategory,
)


def grounded_questions(data):
    """Only explicit cited uncertainty can require a specific unresolved finding.

    Unknown/missing context never proves that a test was not done. A caller must
    explicitly cite a False specificity flag or supply a grounded risk question.
    """
    questions = list(data.grounded_risk_questions)
    selected = {s.interpretation.interpretation_id: s for s in data.selected}
    for context in data.factual_context:
        for observation in context.risk_observations:
            if observation.assessment is RiskAssessment.UNRESOLVED and observation.unresolved:
                questions.append(GroundedRiskQuestion(category=observation.category,
                    interpretation_ids=(context.interpretation_id,),
                    source_references=observation.source_references,
                    resolution_question="What evidence would resolve this cited uncertainty?",
                    severity=observation.severity))
        interpretation = selected[context.interpretation_id].interpretation
        perturbation = (interpretation.design_type.value == "PERTURBATIONAL"
                        and context.intervention_kind in (InterventionKind.GENETIC, InterventionKind.FUNCTIONAL)
                        and context.outcome_relevant is True)
        if not perturbation:
            continue
        for flag, category, question in (
            (context.treatment_specificity_tested is False and context.treatment_phenotype_matches is True,
             RiskCategory.TREATMENT_INDEPENDENT_DEPENDENCY,
             "Does the perturbational effect depend on the relevant treatment?"),
            (context.perturbation_specificity_tested is False,
             RiskCategory.NONSPECIFIC_PERTURBATION,
             "Does a specificity control exclude a nonspecific perturbational effect?"),
        ):
            if flag:
                questions.append(GroundedRiskQuestion(category=category,
                    interpretation_ids=(context.interpretation_id,),
                    source_references=context.source_references,
                    resolution_question=question, severity=DecisionRelevance.MATERIAL))
    return tuple(questions)


def validate_v3_candidate(data, candidate):
    audits = {a.provenance().model_dump_json(): a for a in data.coverage_audits
              if a.coverage == "COMPLETE_FOR_DECLARED_SCOPE"}
    novelty = candidate.novelty
    if any(ref.model_dump_json() not in audits for ref in novelty.search_provenance):
        raise ValueError("v3 novelty.search_provenance must reference the scope of a qualifying "
                         "public-search-coverage-audit-v1 in DecisionInput; retrieved papers or incomplete coverage do not qualify")
    axes = {a.axis: a for a in candidate.axes}
    if novelty.status is not NoveltyStatus.UNKNOWN and not novelty.search_provenance:
        raise ValueError("v3 non-UNKNOWN novelty requires explicit qualifying audit provenance")
    if axes[DecisionAxis.NOVELTY].status is not AxisStatus.UNKNOWN and not novelty.search_provenance:
        raise ValueError("v3 non-UNKNOWN NOVELTY axis requires qualifying audit provenance")

    gaps = {g.gap_id: g for g in candidate.information_gaps}
    next_analysis = candidate.most_decision_changing_next_analysis
    if next_analysis:
        if len(set(next_analysis.gap_ids)) != len(next_analysis.gap_ids):
            raise ValueError("v3 next analysis gap_ids must be unique")
        if len(set(next_analysis.interpretation_ids)) != len(next_analysis.interpretation_ids):
            raise ValueError("v3 next analysis interpretation_ids must be unique")
        union = {iid for gid in next_analysis.gap_ids for iid in gaps[gid].interpretation_ids}
        if set(next_analysis.interpretation_ids) != union:
            raise ValueError("v3 next analysis interpretation_ids must exactly equal the union of selected "
                             f"gap provenance; required={sorted(union)}, supplied={sorted(next_analysis.interpretation_ids)}")
        for gid in next_analysis.gap_ids:
            primary = gaps[gid].primary_analysis_kind
            if primary is None:
                raise ValueError(f"v3 selected gap {gid} must declare primary_analysis_kind")
            if next_analysis.kind is not primary:
                raise ValueError(f"v3 next analysis kind must address gap {gid}'s primary operation {primary.value}; "
                                 "select compatible gaps for one task")

    for finding in candidate.red_team_findings:
        if finding.assessment is RiskAssessment.UNRESOLVED and finding not in data.red_team_review:
            if (not finding.interpretation_ids or not finding.source_references
                    or not finding.resolution_question or not finding.unresolved
                    or finding.severity is DecisionRelevance.UNASSESSED):
                raise ValueError(f"v3 UNRESOLVED finding {finding.finding_id} requires exact interpretation/evidence/source "
                                 "provenance, a resolution_question, qualitative severity and unresolved=true")
    for question in grounded_questions(data):
        refs = {r.model_dump_json() for r in question.source_references}
        if not any(f.category is question.category and f.assessment is RiskAssessment.UNRESOLVED
                   and f.resolution_question and f.unresolved and f.severity is not DecisionRelevance.UNASSESSED
                   and set(question.interpretation_ids).issubset(f.interpretation_ids)
                   and refs.issubset(r.model_dump_json() for r in f.source_references)
                   for f in candidate.red_team_findings):
            raise ValueError(f"v3 grounded uncertainty {question.category.value} for "
                             f"{list(question.interpretation_ids)} requires an explicit UNRESOLVED finding with "
                             "its cited basis and resolution_question; a generic UNASSESSED placeholder is insufficient")

    material_gaps = [g for g in candidate.information_gaps if g.actionable
                     and g.route is EvidenceRoute.COMPUTATIONAL_PUBLIC
                     and g.priority in (DecisionRelevance.MATERIAL, DecisionRelevance.BLOCKING)]
    gap_axis = axes[DecisionAxis.INFORMATION_GAP]
    exception = (candidate.final_decision is DecisionState.COMPUTE_MORE and material_gaps
                 and gap_axis.status is AxisStatus.UNKNOWN)
    if exception and not candidate.information_gap_unknown_rationale:
        raise ValueError("v3 COMPUTE_MORE with MATERIAL/BLOCKING actionable public/computational gaps cannot "
                         "silently leave INFORMATION_GAP UNKNOWN; assess the burden or supply information_gap_unknown_rationale")
    if candidate.information_gap_unknown_rationale and not exception:
        raise ValueError("v3 information_gap_unknown_rationale applies only to UNKNOWN INFORMATION_GAP "
                         "with a material COMPUTE_MORE gap")
    if gap_axis.status is AxisStatus.ABSENT and material_gaps:
        raise ValueError("v3 INFORMATION_GAP ABSENT conflicts with material actionable computational/public gaps")
    if gap_axis.status is AxisStatus.STRONG and not material_gaps:
        raise ValueError("v3 INFORMATION_GAP STRONG requires a material/blocking obtainable computational/public gap")

    active = [f for f in candidate.red_team_findings if f.assessment in
              (RiskAssessment.PRESENT, RiskAssessment.UNRESOLVED)]
    risk_axis = axes[DecisionAxis.CONTRADICTIONS_RISK]
    contradictions = risk_axis.contradictory_interpretation_ids
    if risk_axis.status in (AxisStatus.STRONG, AxisStatus.MODERATE, AxisStatus.WEAK, AxisStatus.MIXED):
        if not active and not contradictions:
            raise ValueError("v3 CONTRADICTIONS_RISK requires a grounded finding or contradiction; "
                             "UNASSESSED categories alone cannot establish risk level")
    if risk_axis.status is AxisStatus.STRONG and not contradictions and not any(
            f.severity in (DecisionRelevance.MATERIAL, DecisionRelevance.BLOCKING) for f in active):
        raise ValueError("v3 STRONG CONTRADICTIONS_RISK requires a grounded material/blocking risk or contradiction")
