"""Reusable connector architecture for public evidence sources.

Every future source (DepMap, GTEx, cBioPortal, GEO) implements the same small
contract, so the pieces a multi-source pipeline depends on are settled once:

* a query is :class:`ConnectorQuery` — the gene plus whichever context axes the
  caller asked about, never merged into one undifferentiated sweep;
* a query outcome is :class:`ConnectorQueryResult` and is always one of exactly
  three states: :attr:`ConnectorStatus.SUCCESS` (results),
  :attr:`ConnectorStatus.NO_RESULTS` (the source answered and there are none),
  or :attr:`ConnectorStatus.FAILURE` (the question was never answered);
* a run outcome is :class:`ConnectorRunResult`, which merges the per-query
  outcomes and keeps provenance that :class:`EvidenceRecord` cannot hold.

Two guarantees make this safe to embed in a pipeline:

1. Expected external failures (transport, HTTP/API, and malformed source
   responses) are returned as ``FAILURE`` results. Programming errors and
   violated internal invariants are allowed to raise so they are not mistaken
   for ordinary source outages.
2. Nothing here interprets science. A connector reports which source answered,
   what was asked, what came back, and when. ``support``, ``directness``,
   ``novelty``, mechanism and effect sizes are never filled in by a connector.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from genedecision.models.evidence import EvidenceRecord, SourceScope

#: Prefix used for every flag this package attaches to a record.
FLAG_CONTEXT_FROM_QUERY = "CONTEXT_FROM_QUERY"
FLAG_SPECIES_NOT_ESTABLISHED = "SPECIES_NOT_ESTABLISHED_BY_SOURCE"
FLAG_MULTI_QUERY_CONFLICT = "MULTI_QUERY_CONFLICT"
FLAG_METADATA_UNAVAILABLE = "PUBLICATION_METADATA_UNAVAILABLE"
FLAG_RESULTS_TRUNCATED = "RESULTS_TRUNCATED_BY_RETMAX"


class ConnectorStatus(StrEnum):
    """The three outcomes a query can have.

    ``NO_RESULTS`` is deliberately not an error: the source was asked and had
    no matching records. ``FAILURE`` means no answer was obtained, so the
    absence of records carries no information at all.
    """

    SUCCESS = "SUCCESS"
    NO_RESULTS = "NO_RESULTS"
    FAILURE = "FAILURE"


class ConnectorFailureKind(StrEnum):
    """Why a query failed. Kept coarse enough to classify, fine enough to act on."""

    TRANSPORT = "TRANSPORT"
    HTTP_STATUS = "HTTP_STATUS"
    RATE_LIMITED = "RATE_LIMITED"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    QUERY_REJECTED = "QUERY_REJECTED"
    INVALID_QUERY = "INVALID_QUERY"
    UNEXPECTED = "UNEXPECTED"


class ConnectorFailure(BaseModel):
    """A failure value carried inside a result instead of being raised."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    connector: str = Field(min_length=1)
    kind: ConnectorFailureKind
    message: str = Field(min_length=1)
    http_status: int | None = None


def utc_now() -> datetime:
    """Timezone-aware current time; the default clock for every connector."""
    return datetime.now(timezone.utc)


class ConnectorQuery(BaseModel):
    """One evidence question put to a connector.

    The gene is always present; at least one context axis must accompany it.
    Asking a source for "the gene, and nothing else" is the single broad sweep
    this design refuses: it collapses disease, phenotype and drug evidence into
    one result set that can no longer be traced back to a specific question.
    Issue separate queries instead, and the run result keeps them apart.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    gene: str = Field(min_length=1)
    disease: str | None = None
    phenotype: str | None = None
    drug: str | None = None

    @model_validator(mode="after")
    def requires_a_context_axis(self) -> "ConnectorQuery":
        if self.disease is None and self.phenotype is None and self.drug is None:
            raise ValueError(
                "a query must combine the gene with at least one of: disease,"
                " phenotype, drug; a gene-only sweep collapses distinct"
                " questions into one untraceable result set"
            )
        return self

    def context_axes(self) -> dict[str, str]:
        """The supplied context axes, in a fixed order, excluding ``None``."""
        return {
            name: value
            for name in ("disease", "phenotype", "drug")
            if (value := getattr(self, name)) is not None
        }


class PublicationMetadata(BaseModel):
    """Publication metadata a literature connector retrieved.

    ``EvidenceRecord`` has a home for the PMID (``source_identifier``), the
    title (``summary``) and the landing page (``source_url``) and nothing else,
    so abstract, journal, date, authors, DOI and publication type are carried
    here rather than being squeezed into an unrelated field or dropped.

    Every value is stored exactly as the source reported it. ``None`` means the
    source did not report that value; nothing is inferred to fill the gap.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_identifier: str | None = None
    title: str | None = None
    abstract: str | None = None
    journal: str | None = None
    publication_date: str | None = None
    publication_year: int | None = None
    authors: list[str] = Field(default_factory=list)
    publication_types: list[str] = Field(default_factory=list)
    doi: str | None = None
    language: str | None = None


class QueryMatch(BaseModel):
    """Persistent provenance linking one evidence item to one exact query.

    Query context is copied from :class:`ConnectorQuery` rather than embedded
    in :class:`EvidenceRecord`. This permits one publication to match several
    exact questions without duplicating the public evidence item.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    query_text: str = Field(min_length=1)
    query_type: str = Field(min_length=1)
    gene: str = Field(min_length=1)
    disease: str | None = None
    phenotype: str | None = None
    drug: str | None = None
    translated_query: str | None = None
    retrieved_at: datetime


class ConnectorQueryResult(BaseModel):
    """What one connector did with one query.

    ``query_string`` is the exact string handed to the source, recorded so the
    question behind any record can be reproduced. It is ``None`` only when no
    string could be rendered at all — a query refused before it was sent.
    ``translated_query`` is the source's own translation of it, when the source
    reports one.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    connector: str = Field(min_length=1)
    query: ConnectorQuery
    query_string: str | None = None
    translated_query: str | None = None
    status: ConnectorStatus
    retrieved_at: datetime

    total_matches: int | None = None
    records: list[EvidenceRecord] = Field(default_factory=list)
    publications: dict[str, PublicationMetadata] = Field(default_factory=dict)
    truncated: bool = False
    error: ConnectorFailure | None = None

    @model_validator(mode="after")
    def status_matches_payload(self) -> "ConnectorQueryResult":
        if self.status is ConnectorStatus.FAILURE:
            if self.error is None:
                raise ValueError("a FAILURE result must carry an error")
            if self.records:
                raise ValueError(
                    "a FAILURE result must not carry records; a partial or"
                    " unverifiable response is reported as a failure instead"
                )
        elif self.error is not None:
            raise ValueError(
                f"a {self.status.value} result must not carry an error"
            )
        if self.status is ConnectorStatus.SUCCESS and not self.records:
            raise ValueError("a SUCCESS result must carry at least one record")
        if self.status is ConnectorStatus.NO_RESULTS and self.records:
            raise ValueError("a NO_RESULTS result must not carry records")
        if self.query_string is None and self.status is not ConnectorStatus.FAILURE:
            raise ValueError(
                "a non-failure result must record the exact query string that"
                " produced it"
            )
        unknown = sorted(set(self.publications) - {r.evidence_id for r in self.records})
        if unknown:
            raise ValueError(
                "publication metadata is attached to evidence ids that are not"
                " in this result: " + ", ".join(unknown)
            )
        return self

    @property
    def returned_count(self) -> int:
        return len(self.records)


def _merge_records(
    results: Sequence[ConnectorQueryResult],
) -> tuple[list[EvidenceRecord], dict[str, list[str]]]:
    """Collapse duplicate evidence ids, keeping every matched query.

    The same publication can answer several of the queries in one run. It stays
    a **single** evidence item, so a record is never double-counted as two
    independent observations. The first query that produced it supplies the
    values; every later query that produced it is recorded in the match map,
    and any field the later records disagree about is named in a
    ``MULTI_QUERY_CONFLICT:<field>`` flag rather than being resolved silently.

    Ordering is by ``evidence_id``, so the merged list is stable no matter what
    order the source returned its records in.
    """
    by_id: dict[str, EvidenceRecord] = {}
    matched: dict[str, list[str]] = {}
    for result in results:
        for record in result.records:
            queries = matched.setdefault(record.evidence_id, [])
            if result.query_string not in queries:
                queries.append(result.query_string)
            existing = by_id.get(record.evidence_id)
            by_id[record.evidence_id] = (
                record if existing is None else _reconcile(existing, record)
            )
    return [by_id[key] for key in sorted(by_id)], matched


def _reconcile(kept: EvidenceRecord, other: EvidenceRecord) -> EvidenceRecord:
    """Keep the first record's values, union its flags, name any disagreement."""
    conflicts: list[str] = []
    for name in EvidenceRecord.model_fields:
        if name == "quality_flags":
            continue
        if getattr(kept, name) != getattr(other, name):
            conflicts.append(name)

    flags = list(kept.quality_flags)
    for flag in [*other.quality_flags, *(f"{FLAG_MULTI_QUERY_CONFLICT}:{c}" for c in conflicts)]:
        if flag not in flags:
            flags.append(flag)
    return kept.model_copy(update={"quality_flags": flags})


class ConnectorRunResult(BaseModel):
    """The outcome of running one connector over several queries.

    ``query_matches`` is the query provenance: it maps each evidence id to the
    exact query strings that retrieved it, in the order the queries were asked.
    ``publications`` carries the per-record metadata ``EvidenceRecord`` has no
    field for.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    connector: str = Field(min_length=1)
    source_scope: SourceScope
    retrieved_at: datetime
    query_results: list[ConnectorQueryResult] = Field(min_length=1)
    records: list[EvidenceRecord] = Field(default_factory=list)
    publications: dict[str, PublicationMetadata] = Field(default_factory=dict)
    query_matches: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def records_are_traceable(self) -> "ConnectorRunResult":
        ids = {record.evidence_id for record in self.records}
        untraceable = sorted(ids - set(self.query_matches))
        if untraceable:
            raise ValueError(
                "every record must record which query produced it; missing:"
                " " + ", ".join(untraceable)
            )
        orphan_metadata = sorted(set(self.publications) - ids)
        if orphan_metadata:
            raise ValueError(
                "publication metadata without a record: "
                + ", ".join(orphan_metadata)
            )
        return self

    @property
    def status(self) -> ConnectorStatus:
        """One status for the whole run.

        Any successful query makes the run successful; otherwise a query that
        was answered with nothing makes it ``NO_RESULTS``; if every query
        failed, the run failed. Per-query detail is always available in
        ``query_results`` and is the authority for which query failed.
        """
        statuses = [result.status for result in self.query_results]
        if ConnectorStatus.SUCCESS in statuses:
            return ConnectorStatus.SUCCESS
        if ConnectorStatus.NO_RESULTS in statuses:
            return ConnectorStatus.NO_RESULTS
        return ConnectorStatus.FAILURE

    @property
    def failures(self) -> list[ConnectorQueryResult]:
        """The queries this connector could not answer."""
        return [
            result
            for result in self.query_results
            if result.status is ConnectorStatus.FAILURE
        ]

    def matched_queries(self, evidence_id: str) -> list[str]:
        """The exact query strings that retrieved one record."""
        return list(self.query_matches.get(evidence_id, []))

    def publication(self, evidence_id: str) -> PublicationMetadata | None:
        """The retrieved publication metadata for one record, if any."""
        return self.publications.get(evidence_id)

    def query_match_records(self) -> list[QueryMatch]:
        """Materialize the per-record query links for durable storage."""
        matches: list[QueryMatch] = []
        seen: set[tuple[str, str]] = set()
        for result in self.query_results:
            if result.query_string is None:
                continue
            query_type = "+".join(("gene", *result.query.context_axes()))
            for record in result.records:
                key = (record.evidence_id, result.query_string)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    QueryMatch(
                        run_id=self.run_id,
                        evidence_id=record.evidence_id,
                        connector=self.connector,
                        query_text=result.query_string,
                        query_type=query_type,
                        gene=result.query.gene,
                        disease=result.query.disease,
                        phenotype=result.query.phenotype,
                        drug=result.query.drug,
                        translated_query=result.translated_query,
                        retrieved_at=result.retrieved_at,
                    )
                )
        return matches


class EvidenceConnector(ABC):
    """Base class for every public evidence connector.

    Subclasses implement :meth:`build_query_string` (how a question becomes a
    source-specific string) and :meth:`search` (how that string is executed).
    :meth:`search` must turn expected external failures into ``FAILURE``
    results. Unexpected exceptions must escape so programming errors and
    violated invariants remain visible.
    """

    #: Name recorded on every result and in ``source_name`` of each record.
    name: str = "connector"
    #: Whether this connector's evidence is PUBLIC (all current ones) or not.
    source_scope: SourceScope = SourceScope.PUBLIC

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or utc_now

    @property
    def clock(self) -> Callable[[], datetime]:
        return self._clock

    def now(self) -> datetime:
        """Current time from the injected clock (UTC by default)."""
        return self._clock()

    @abstractmethod
    def build_query_string(self, query: ConnectorQuery) -> str:
        """Render the query as the exact string the source will be asked."""

    @abstractmethod
    def search(
        self,
        query: ConnectorQuery,
        *,
        retrieved_at: datetime | None = None,
    ) -> ConnectorQueryResult:
        """Execute one query; classify expected source failures as ``FAILURE``."""

    def failure_result(
        self,
        query: ConnectorQuery,
        *,
        kind: ConnectorFailureKind,
        message: str,
        retrieved_at: datetime,
        query_string: str | None = None,
        http_status: int | None = None,
    ) -> ConnectorQueryResult:
        """Build a ``FAILURE`` result for one query.

        When ``query_string`` is not supplied the query is rendered, and if it
        cannot be rendered the result simply records no string rather than
        inventing one.
        """
        if query_string is None:
            try:
                query_string = self.build_query_string(query)
            except ValueError:
                query_string = None
        return ConnectorQueryResult(
            connector=self.name,
            query=query,
            query_string=query_string,
            status=ConnectorStatus.FAILURE,
            retrieved_at=retrieved_at,
            error=ConnectorFailure(
                connector=self.name,
                kind=kind,
                message=message,
                http_status=http_status,
            ),
        )

    def search_many(self, queries: Sequence[ConnectorQuery]) -> ConnectorRunResult:
        """Run several queries and merge them into one traceable run result.

        All queries in a run share one retrieval timestamp, so the records a
        duplicate publication collapses into never disagree about when they
        were fetched.
        """
        if not queries:
            raise ValueError("a run must contain at least one query")
        retrieved_at = self.now()
        results = [
            self.search(query, retrieved_at=retrieved_at) for query in queries
        ]
        records, matched = _merge_records(results)
        publications = {
            evidence_id: publication
            for result in results
            for evidence_id, publication in result.publications.items()
        }
        return ConnectorRunResult(
            connector=self.name,
            source_scope=self.source_scope,
            retrieved_at=retrieved_at,
            query_results=results,
            records=records,
            publications=publications,
            query_matches=matched,
        )


def collect_connector_runs(
    connectors: Iterable[EvidenceConnector],
    queries: Sequence[ConnectorQuery],
    *,
    clock: Callable[[], datetime] | None = None,
) -> list[ConnectorRunResult]:
    """Run several connectors over the same queries.

    Returns one run per connector, in the order supplied. A connector that
    fails — because its source is down, rate-limited, or returns something
    unparseable — contributes a ``FAILURE`` run and the remaining connectors
    still run. Use :meth:`ConnectorRunResult.failures` and
    :attr:`ConnectorRunResult.status` to see which sources answered;
    nothing is silently retried or substituted.
    """
    if not queries:
        raise ValueError("a run must contain at least one query")
    # Retained for API compatibility with 3A. Connector clocks determine each
    # run timestamp; this clock is intentionally not an exception fallback.
    _ = clock
    return [connector.search_many(queries) for connector in connectors]
