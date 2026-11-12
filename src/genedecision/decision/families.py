"""Conservative provenance families; distinct rows never establish independence."""

from enum import StrEnum
from itertools import combinations

from genedecision.models.decision_core import FrozenModel, SelectedInterpretation, EvidenceContext


class IndependenceStatus(StrEnum):
    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    SHARED_FAMILY = "SHARED_FAMILY"
    DISTINCT_SOURCE = "DISTINCT_SOURCE"


class SynthesisItem(FrozenModel):
    # Retains all interpretation labels, provider/version, hypothesis and source identity.
    selected: SelectedInterpretation
    context: EvidenceContext | None = None
    family_id: str
    lineage_keys: tuple[str, ...]
    independence: IndependenceStatus

    @property
    def iid(self):
        return self.selected.interpretation.interpretation_id

    @property
    def judgment(self):
        return self.selected.interpretation


def normalize_synthesis(data):
    contexts = {c.interpretation_id: c for c in data.factual_context}
    keys = {}
    lineage = {}
    for s in data.selected:
        iid = s.interpretation.interpretation_id
        c = contexts.get(iid)
        # Scope is deliberately not prefixed: shared publication/dataset origins
        # across USER and PUBLIC are still non-independent.
        origins = set(s.provenance.origin_keys)
        aliases = set()
        if s.provenance.source_identifier:
            # An identifier can merge known repetitions, but different generic
            # record IDs cannot prove that USER/public datasets are independent.
            aliases.add("SOURCE_ALIAS:" + (s.provenance.source_name or s.provenance.source_type)
                        + ":" + s.provenance.source_identifier)
        if c and c.independence_group:
            origins.add("DATASET:" + c.independence_group)
        if s.provenance.source_kind.value == "DEPMAP":
            origins.update("DEPMAP_RELEASE:" + r.record_id for r in s.provenance.source_records
                           if r.record_type == "DepMapReleaseMetadata")
            if not origins and s.provenance.source_identifier:
                origins.add("DEPMAP_PROFILE:" + s.provenance.source_identifier)
        lineage[iid] = origins
        keys[iid] = origins | aliases | {"EVIDENCE:" + s.provenance.evidence_id}
    groups = [{s.interpretation.interpretation_id} for s in data.selected]
    # Transitive union handles publication aliases without inventing deduplication.
    for a, b in combinations(data.selected, 2):
        ia, ib = a.interpretation.interpretation_id, b.interpretation.interpretation_id
        if keys[ia] & keys[ib]:
            ga = next(g for g in groups if ia in g)
            gb = next(g for g in groups if ib in g)
            if ga is not gb:
                ga.update(gb)
                groups.remove(gb)
    items = []
    for s in data.selected:
        iid = s.interpretation.interpretation_id
        group = next(g for g in groups if iid in g)
        status = IndependenceStatus.SHARED_FAMILY if group != {iid} else (
            IndependenceStatus.DISTINCT_SOURCE if lineage[iid] else IndependenceStatus.NOT_ESTABLISHED)
        items.append(SynthesisItem(selected=s, context=contexts.get(iid), family_id=min(group),
                                   lineage_keys=tuple(sorted(lineage[iid])), independence=status))
    return tuple(items)


def distinct_sources(a, b):
    """A bounded source-lineage distinction, not proof of biological independence."""
    return bool(a.lineage_keys and b.lineage_keys and a.family_id != b.family_id)


def complementary_sources(items):
    return any(distinct_sources(a, b) and a.context and b.context
               and a.context.modality and b.context.modality
               and a.context.modality != b.context.modality for a, b in combinations(items, 2))
