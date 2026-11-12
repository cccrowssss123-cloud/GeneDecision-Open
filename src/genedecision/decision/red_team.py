"""Bounded structural screen; unsupported biological confounders stay unassessed."""

from itertools import combinations

from genedecision.models.decision_core import (
    DecisionRelevance, InterventionKind, RedTeamFinding, RiskAssessment, RiskCategory,
)


def screen_red_team(selected, contexts):
    by_context = {c.interpretation_id: c for c in contexts}
    findings = []
    by_id = {s.interpretation.interpretation_id: s for s in selected}

    def add(category, ids, explanation, severity=DecisionRelevance.MATERIAL,
            assessment=RiskAssessment.PRESENT, unresolved=True, source_references=()):
        if not source_references:
            cited = {}
            for iid in sorted(set(ids)):
                context = by_context.get(iid)
                refs = by_id[iid].interpretation.rationale_references + (context.source_references if context else ())
                for ref in refs:
                    cited[ref.model_dump_json()] = ref
            source_references = tuple(cited[k] for k in sorted(cited))
        findings.append(RedTeamFinding(
            finding_id=f"RT_{len(findings):03d}_{category.value}", category=category,
            explanation=explanation, interpretation_ids=tuple(sorted(set(ids))),
            evidence_ids=tuple(sorted({by_id[i].provenance.evidence_id for i in ids})),
            severity=severity, assessment=assessment, unresolved=unresolved,
            source_references=source_references))

    negative = [i for i,s in by_id.items() if s.interpretation.support.value == "CONTRADICT"]
    if negative:
        add(RiskCategory.CONTRADICTORY_EVIDENCE, negative,
            "Selected interpretations contradict the confirmed hypothesis; scope and relevance remain explicit.")
        add(RiskCategory.OPPOSITE_DIRECTION, negative,
            "Opposite-direction judgments are present in the selected interpretation set.")
    association = [i for i,s in by_id.items() if s.interpretation.support.value == "SUPPORT"
                   and s.interpretation.design_type.value in ("ASSOCIATIONAL", "OBSERVATIONAL")]
    if association:
        add(RiskCategory.OBSERVATIONAL_CAUSAL_MISMATCH, association,
            "Directional observational/associational support does not by itself establish a causal contribution.")
    duplicate_ids = set()
    for a,b in combinations(selected, 2):
        ia, ib = a.interpretation.interpretation_id, b.interpretation.interpretation_id
        ca, cb = by_context.get(ia), by_context.get(ib)
        same_group = ca and cb and ca.independence_group and ca.independence_group == cb.independence_group
        if a.provenance.evidence_id == b.provenance.evidence_id or set(a.provenance.origin_keys) & set(b.provenance.origin_keys) or same_group:
            duplicate_ids.update((ia, ib))
    if duplicate_ids:
        add(RiskCategory.DUPLICATED_NONINDEPENDENT_EVIDENCE, duplicate_ids,
            "These selections share an evidence item, publication origin or cited dataset group; they are not separate confirmations.")
    for category, field, message in (
        (RiskCategory.INSUFFICIENT_COMPARATOR, "comparator_identified", "Cited context reports an insufficient comparator."),
        (RiskCategory.DISEASE_MODEL_MISMATCH, "model_matches", "Cited context reports a disease/model mismatch."),
        (RiskCategory.TREATMENT_PHENOTYPE_MISMATCH, "treatment_phenotype_matches", "Cited context reports a treatment/phenotype mismatch."),
    ):
        ids = [c.interpretation_id for c in contexts if getattr(c, field) is False]
        if ids:
            add(category, ids, message)
    causal = [i for i,s in by_id.items() if s.interpretation.directness.value == "DIRECT"
              and s.interpretation.design_type.value == "PERTURBATIONAL" and i in by_context
              and by_context[i].intervention_kind in (InterventionKind.GENETIC, InterventionKind.FUNCTIONAL)
              and all(getattr(by_context[i], f) is True for f in (
                  "comparator_identified", "outcome_relevant", "model_matches", "treatment_phenotype_matches"))]
    if not causal:
        add(RiskCategory.MISSING_DIRECT_CAUSAL_TEST, list(by_id),
            "The selected set lacks a direct perturbational test with cited intervention, comparator, relevant outcome and matched context.")
    for context in sorted(contexts, key=lambda c: c.interpretation_id):
        for observation in context.risk_observations:
            add(observation.category, [context.interpretation_id], observation.explanation,
                observation.severity, assessment=observation.assessment,
                unresolved=observation.unresolved if observation.assessment is not RiskAssessment.ABSENT else False,
                source_references=observation.source_references)
    present = {f.category for f in findings}
    for category in RiskCategory:
        if category not in present:
            add(category, [], "This category cannot be assessed from the selected judgments and supplied cited factual context; absence of a finding is not evidence of absence.",
                DecisionRelevance.UNASSESSED, RiskAssessment.UNASSESSED)
    return tuple(findings)
