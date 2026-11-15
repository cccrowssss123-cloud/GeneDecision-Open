"""Union of independently evaluated local Discovery runs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

from pydantic import model_validator

from .contracts import Declaration, Digest, Text
from .models import DatasetManifest, RowLocator
from .run import CandidateKey, DiscoveryRun, DiscoveryRunError


class UnionRun(Declaration):
    run_id: Text
    dataset_id: Text
    source_sha256: Digest


class CandidateRunEvidence(Declaration):
    run_id: Text
    matching_rows: tuple[RowLocator, ...]
    nonmatching_rows: tuple[RowLocator, ...]
    unknown_rows: tuple[RowLocator, ...]


class UnionCandidate(Declaration):
    key: CandidateKey
    evidence: tuple[CandidateRunEvidence, ...]
    warning: Literal["SCIENTIFICALLY_UNVERIFIED"] = "SCIENTIFICALLY_UNVERIFIED"


class DiscoveryUnion(Declaration):
    schema_version: Literal["candidate-discovery-union-v1"] = "candidate-discovery-union-v1"
    data_classification: Literal["LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"] = "LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD"
    scientific_validation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    warning: Literal["SCIENTIFICALLY_UNVERIFIED"] = "SCIENTIFICALLY_UNVERIFIED"
    independence: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    runs: tuple[UnionRun, ...]
    candidates: tuple[UnionCandidate, ...]
    union_id: Digest

    @model_validator(mode="after")
    def check_content_digest(self) -> DiscoveryUnion:
        payload = self.model_dump(mode="json", exclude={"union_id"})
        if self.union_id != DatasetManifest.content_digest(payload):
            raise ValueError("DISCOVERY_UNION_ID_MISMATCH")
        return self


def _key_tuple(key: CandidateKey) -> tuple[str, str, str]:
    return key.taxon, key.namespace, key.identifier


def union_discovery_runs(runs: Iterable[DiscoveryRun]) -> DiscoveryUnion:
    """Keep a key that matched in any run and retain its other observed rows.

    A run without rows for a key contributes no evidence for that key. The view
    makes no row-level absence claim. Repeated source bytes retain the same
    source hash, without establishing study independence.
    """
    index = {}
    try:
        for run in runs:
            validated = DiscoveryRun.model_validate(run.model_dump(mode="python", warnings=False))
            index.setdefault(validated.run_id, validated)
    except (AttributeError, ValueError, TypeError, RecursionError):
        raise DiscoveryRunError("INVALID_DISCOVERY_RUN_FOR_UNION") from None
    if not index:
        raise DiscoveryRunError("DISCOVERY_UNION_REQUIRES_RUN")

    keys = {_key_tuple(candidate.key): candidate.key for run in index.values() for candidate in run.candidates}
    evidence = defaultdict(list)
    sources = []
    for run_id, run in sorted(index.items()):
        manifest = run.identifiers.inspection.manifest
        sources.append(UnionRun(run_id=run_id, dataset_id=manifest.dataset_id,
                                source_sha256=manifest.source_file_sha256))
        rows_by_key = defaultdict(lambda: {"T": [], "F": [], "U": []})
        for row in run.observations:
            if row.canonical_key is not None:
                key = _key_tuple(row.canonical_key)
                if key in keys:
                    rows_by_key[key][row.truth].append(row.locator)
        for key, partitions in rows_by_key.items():
            evidence[key].append(CandidateRunEvidence(
                run_id=run_id, matching_rows=tuple(partitions["T"]),
                nonmatching_rows=tuple(partitions["F"]), unknown_rows=tuple(partitions["U"]),
            ))
    candidates = tuple(UnionCandidate(key=keys[key], evidence=tuple(evidence[key])) for key in sorted(keys))
    payload = DiscoveryUnion.model_construct(
        runs=tuple(sources), candidates=candidates, union_id="pending",
    ).model_dump(mode="json", exclude={"union_id"})
    return DiscoveryUnion(**payload, union_id=DatasetManifest.content_digest(payload))
