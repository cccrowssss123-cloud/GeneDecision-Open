"""PubMed connector, built on the official NCBI E-utilities.

Three official endpoints are used, in the order NCBI documents for a search:

1. ``esearch.fcgi`` (``db=pubmed``) returns the PMIDs matching a query;
2. ``esummary.fcgi`` (``db=pubmed``) returns the retrieved publication metadata
   for those PMIDs;
3. ``efetch.fcgi`` (``db=pubmed``, ``retmode=xml``) returns the source records,
   including abstracts when PubMed supplies them.

No HTML page is scraped and no unofficial interface is called.

What the connector does
-----------------------
* turns a :class:`~genedecision.connectors.base.ConnectorQuery` into an exact
  PubMed term string and records that string;
* keeps the PMID as ``source_identifier`` and the canonical PubMed URL as
  ``source_url``;
* normalizes each retrieved publication into the existing PUBLIC
  ``EvidenceRecord`` schema, with ``support = UNKNOWN`` and
  ``directness = UNASSESSED`` because a connector reports evidence, not a
  judgement about it;
* reports which query retrieved each record, and collapses a publication that
  matched several queries into one evidence item.

What the connector deliberately does not do
-------------------------------------------
* It stores abstract text as publication metadata but never interprets it.
  ``effect_value``, ``p_value``, ``fdr`` and ``sample_size`` stay ``None``;
  a number in an abstract is never restated as a scientific fact here.
* It never infers ``support``, ``directness``, ``evidence_level``, novelty,
  contradiction, mechanism, or experimental value.
* It never fills a missing metadata value with a guess: a value the source did
  not report stays ``None``.

The disease, phenotype and drug fields on each record restate the *query axes*
that retrieved it, not a claim extracted from the publication; the
``CONTEXT_FROM_QUERY`` quality flag says so on every record.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from genedecision.connectors.base import (
    FLAG_CONTEXT_FROM_QUERY,
    FLAG_METADATA_UNAVAILABLE,
    FLAG_RESULTS_TRUNCATED,
    FLAG_SPECIES_NOT_ESTABLISHED,
    ConnectorFailureKind,
    ConnectorQuery,
    ConnectorQueryResult,
    ConnectorStatus,
    EvidenceConnector,
    PublicationMetadata,
)
from genedecision.connectors.transport import (
    HttpTransport,
    TransportError,
    TransportResponse,
    UrllibTransport,
)
from genedecision.models.evidence import (
    Directness,
    EvidenceLevel,
    EvidenceRecord,
    SourceScope,
    Support,
)

#: Official NCBI E-utilities base URL.
EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
ESEARCH_URL = f"{EUTILS_BASE_URL}esearch.fcgi"
ESUMMARY_URL = f"{EUTILS_BASE_URL}esummary.fcgi"
EFETCH_URL = f"{EUTILS_BASE_URL}efetch.fcgi"
#: Canonical landing page for a PMID.
PUBMED_RECORD_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

SOURCE_NAME = "PubMed"
SOURCE_TYPE = "LITERATURE"

#: Field restriction applied to every query term. ``[Title/Abstract]`` keeps a
#: hit meaningful for the question asked, instead of matching a term that only
#: appears in an unrelated part of a record.
DEFAULT_TERM_FIELD = "Title/Abstract"
DEFAULT_RETMAX = 20
DEFAULT_SORT = "relevance"
DEFAULT_TOOL = "GeneDecision"

#: PMIDs are digit strings; anything else is not an identifier PubMed issues.
_PMID_PATTERN = re.compile(r"^[0-9]+$")
#: A publication date such as "2020 Jan 15" starts with its year.
_YEAR_PATTERN = re.compile(r"^[0-9]{4}")
#: Characters that would let a caller-supplied term restructure the query.
_RESERVED_TERM_CHARACTERS = ('"', "[", "]")


class _RequestFailure(Exception):
    """Internal: a request or an unparseable response, and how to report it."""

    def __init__(
        self,
        kind: ConnectorFailureKind,
        message: str,
        *,
        http_status: int | None = None,
    ) -> None:
        self.kind = kind
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class PubMedConnector(EvidenceConnector):
    """Search PubMed and normalize the results into ``EvidenceRecord`` rows."""

    name = SOURCE_NAME
    source_scope = SourceScope.PUBLIC

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        email: str | None = None,
        api_key: str | None = None,
        tool: str = DEFAULT_TOOL,
        retmax: int = DEFAULT_RETMAX,
        sort: str | None = DEFAULT_SORT,
        term_field: str | None = DEFAULT_TERM_FIELD,
        timeout: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Configure the connector.

        ``transport`` is the only network seam and is injectable, so unit tests
        never reach the internet. ``email``/``api_key`` fall back to the
        ``GENE_DECISION_NCBI_EMAIL`` (or ``NCBI_EMAIL``) and ``NCBI_API_KEY``
        environment variables; NCBI asks callers to identify themselves, and an
        API key raises the permitted request rate. ``retmax`` bounds how many
        PMIDs a single query retrieves; when a query matches more, the result is
        marked ``truncated`` rather than silently presented as complete.
        """
        super().__init__(clock=clock)
        self._transport = transport or UrllibTransport(
            timeout=15.0 if timeout is None else timeout
        )
        self._email = email or os.environ.get("GENE_DECISION_NCBI_EMAIL") or os.environ.get(
            "NCBI_EMAIL"
        )
        self._api_key = api_key or os.environ.get("NCBI_API_KEY")
        self._tool = tool
        self._retmax = retmax
        self._sort = sort
        self._term_field = term_field
        self._timeout = timeout

    # -- query rendering ---------------------------------------------------

    def build_query_string(self, query: ConnectorQuery) -> str:
        """Render the query as the exact PubMed term string that will be sent.

        Each supplied axis becomes one quoted phrase restricted to the
        configured field, and the axes are combined with ``AND``. An axis that
        contains a character able to restructure the query (``"``, ``[``, ``]``)
        is refused rather than escaped or trimmed, so what is recorded is
        always exactly what was asked.
        """
        field = f"[{self._term_field}]" if self._term_field else ""
        terms: list[str] = []
        for axis, value in (("gene", query.gene), *query.context_axes().items()):
            term = value.strip()
            if not term:
                raise ValueError(f"query axis {axis!r} is empty")
            offending = [c for c in _RESERVED_TERM_CHARACTERS if c in term]
            if offending:
                raise ValueError(
                    f"query axis {axis!r} contains reserved character(s) "
                    + ", ".join(repr(c) for c in offending)
                    + "; the term is refused rather than rewritten"
                )
            terms.append(f'"{term}"{field}')
        return " AND ".join(terms)

    # -- execution ---------------------------------------------------------

    def search(
        self,
        query: ConnectorQuery,
        *,
        retrieved_at: datetime | None = None,
    ) -> ConnectorQueryResult:
        """Ask PubMed one question; expected external failures become statuses."""
        timestamp = retrieved_at if retrieved_at is not None else self.now()
        try:
            query_string = self.build_query_string(query)
        except ValueError as exc:
            return self.failure_result(
                query,
                kind=ConnectorFailureKind.INVALID_QUERY,
                message=str(exc),
                retrieved_at=timestamp,
                query_string=None,
            )

        try:
            esearch = self._get_json(
                ESEARCH_URL, self._esearch_params(query_string), what="esearch"
            )
            pmids, total_matches = self._parse_esearch(esearch)
            if not pmids:
                if total_matches:
                    raise _RequestFailure(
                        ConnectorFailureKind.MALFORMED_RESPONSE,
                        "esearch reported"
                        f" {total_matches} matching record(s) but returned no"
                        " PMIDs; the response is inconsistent and is not"
                        " reported as a zero-result query",
                    )
                return ConnectorQueryResult(
                    connector=self.name,
                    query=query,
                    query_string=query_string,
                    translated_query=self._translated_query(esearch),
                    status=ConnectorStatus.NO_RESULTS,
                    retrieved_at=timestamp,
                    total_matches=total_matches,
                )

            esummary = self._get_json(
                ESUMMARY_URL, self._esummary_params(pmids), what="esummary"
            )
            summaries = self._parse_esummary(esummary, pmids)
            efetch = self._get_text(
                EFETCH_URL, self._efetch_params(pmids), what="efetch"
            )
            abstracts = self._parse_efetch_abstracts(efetch, pmids)
        except _RequestFailure as failure:
            return self.failure_result(
                query,
                kind=failure.kind,
                message=failure.message,
                retrieved_at=timestamp,
                query_string=query_string,
                http_status=failure.http_status,
            )

        truncated = bool(total_matches and total_matches > len(pmids))
        records: list[EvidenceRecord] = []
        publications: dict[str, PublicationMetadata] = {}
        for pmid in pmids:
            summary = summaries.get(pmid)
            metadata = self._publication_metadata(
                pmid, summary, abstract=abstracts.get(pmid)
            )
            record = self._build_record(
                query,
                pmid,
                metadata,
                retrieved_at=timestamp,
                truncated=truncated,
                metadata_missing=summary is None,
            )
            records.append(record)
            publications[record.evidence_id] = metadata

        return ConnectorQueryResult(
            connector=self.name,
            query=query,
            query_string=query_string,
            translated_query=self._translated_query(esearch),
            status=ConnectorStatus.SUCCESS,
            retrieved_at=timestamp,
            total_matches=total_matches,
            records=records,
            publications=publications,
            truncated=truncated,
        )

    # -- request plumbing --------------------------------------------------

    def _esearch_params(self, query_string: str) -> dict[str, str]:
        params = {
            "db": "pubmed",
            "term": query_string,
            "retmode": "json",
            "retmax": str(self._retmax),
        }
        if self._sort:
            params["sort"] = self._sort
        return params | self._identity_params()

    def _esummary_params(self, pmids: Sequence[str]) -> dict[str, str]:
        return {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "json",
        } | self._identity_params()

    def _efetch_params(self, pmids: Sequence[str]) -> dict[str, str]:
        return {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
        } | self._identity_params()

    def _identity_params(self) -> dict[str, str]:
        """NCBI's requested caller identification; the API key is never recorded."""
        params = {"tool": self._tool}
        if self._email:
            params["email"] = self._email
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    def _get_json(
        self, url: str, params: Mapping[str, str], *, what: str
    ) -> Any:
        """Perform one request and decode it, or raise :class:`_RequestFailure`."""
        try:
            response = self._transport.get(url, params, timeout=self._timeout)
        except TransportError as exc:
            raise _RequestFailure(ConnectorFailureKind.TRANSPORT, str(exc)) from exc

        failure = self._classify_status(response, what=what)
        if failure is not None:
            raise failure

        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                f"{what} returned a body that is not valid JSON: {exc.msg}",
                http_status=response.status_code,
            ) from exc

        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            raise _RequestFailure(
                ConnectorFailureKind.QUERY_REJECTED,
                f"{what} rejected the query: {payload['error']}",
                http_status=response.status_code,
            )
        return payload

    def _get_text(
        self, url: str, params: Mapping[str, str], *, what: str
    ) -> str:
        """Perform one request and return its body after status validation."""
        try:
            response = self._transport.get(url, params, timeout=self._timeout)
        except TransportError as exc:
            raise _RequestFailure(ConnectorFailureKind.TRANSPORT, str(exc)) from exc

        failure = self._classify_status(response, what=what)
        if failure is not None:
            raise failure
        return response.body

    def _classify_status(
        self, response: TransportResponse, *, what: str
    ) -> _RequestFailure | None:
        status = response.status_code
        if status == 429:
            return _RequestFailure(
                ConnectorFailureKind.RATE_LIMITED,
                f"{what} was rate limited (HTTP 429); NCBI permits 3 requests"
                " per second without an API key",
                http_status=status,
            )
        if status != 200:
            return _RequestFailure(
                ConnectorFailureKind.HTTP_STATUS,
                f"{what} returned HTTP {status}",
                http_status=status,
            )
        return None

    # -- response parsing --------------------------------------------------

    def _parse_esearch(self, payload: Any) -> tuple[list[str], int | None]:
        """Extract the PMIDs and the source-reported match count."""
        if not isinstance(payload, dict):
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                "esearch response is not a JSON object",
            )
        result = payload.get("esearchresult")
        if not isinstance(result, dict):
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                "esearch response has no 'esearchresult' object",
            )
        raw_ids = result.get("idlist")
        if not isinstance(raw_ids, list):
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                "esearch response has no 'idlist' array",
            )

        pmids: list[str] = []
        for raw in raw_ids:
            pmid = raw if isinstance(raw, str) else str(raw)
            if not _PMID_PATTERN.match(pmid):
                raise _RequestFailure(
                    ConnectorFailureKind.MALFORMED_RESPONSE,
                    f"esearch returned {raw!r}, which is not a PMID",
                )
            if pmid not in pmids:
                pmids.append(pmid)

        return pmids, self._total_matches(result)

    @staticmethod
    def _total_matches(result: Mapping[str, Any]) -> int | None:
        count = result.get("count")
        try:
            return int(str(count))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _translated_query(payload: Any) -> str | None:
        """The source's own translation of the query, when it reports one."""
        if not isinstance(payload, dict):
            return None
        result = payload.get("esearchresult")
        if not isinstance(result, dict):
            return None
        translation = result.get("querytranslation")
        return translation if isinstance(translation, str) else None

    def _parse_esummary(
        self, payload: Any, requested: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Map each requested PMID to its summary object.

        A PMID with no entry is left out and flagged on the record; a response
        whose envelope is unreadable fails the whole query, because a partial
        payload cannot be told apart from a silently truncated one.
        """
        if not isinstance(payload, dict):
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                "esummary response is not a JSON object",
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                "esummary response has no 'result' object",
            )
        if not isinstance(result.get("uids"), list):
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                "esummary response has no 'uids' array",
            )

        summaries: dict[str, dict[str, Any]] = {}
        for pmid in requested:
            entry = result.get(pmid)
            if entry is None:
                continue
            if not isinstance(entry, dict):
                raise _RequestFailure(
                    ConnectorFailureKind.MALFORMED_RESPONSE,
                    f"esummary entry for PMID {pmid} is not a JSON object",
                )
            summaries[pmid] = entry
        return summaries

    def _parse_efetch_abstracts(
        self, body: str, requested: Sequence[str]
    ) -> dict[str, str]:
        """Return source abstract text keyed by PMID; absence remains absence."""
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                f"efetch returned invalid XML: {exc}",
            ) from exc
        if _xml_local_name(root.tag) != "PubmedArticleSet":
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                "efetch response root is not 'PubmedArticleSet'",
            )
        source_error = root.find(".//ERROR")
        if source_error is not None:
            raise _RequestFailure(
                ConnectorFailureKind.QUERY_REJECTED,
                f"efetch rejected the request: {_xml_text(source_error) or 'unknown error'}",
            )

        abstracts: dict[str, str] = {}
        returned: set[str] = set()
        for article in root.findall(".//PubmedArticle"):
            pmid_node = article.find("./MedlineCitation/PMID")
            pmid = _xml_text(pmid_node)
            if pmid is None or not _PMID_PATTERN.match(pmid):
                raise _RequestFailure(
                    ConnectorFailureKind.MALFORMED_RESPONSE,
                    "efetch article has no valid PMID",
                )
            returned.add(pmid)
            sections: list[str] = []
            for node in article.findall(
                "./MedlineCitation/Article/Abstract/AbstractText"
            ):
                text = _xml_text(node)
                if text is None:
                    continue
                label = node.attrib.get("Label")
                sections.append(f"{label}: {text}" if label else text)
            if sections:
                abstracts[pmid] = "\n\n".join(sections)
        missing = sorted(set(requested) - returned)
        if missing:
            raise _RequestFailure(
                ConnectorFailureKind.MALFORMED_RESPONSE,
                "efetch response omitted requested PMID(s): " + ", ".join(missing),
            )
        return abstracts

    # -- normalization -----------------------------------------------------

    def _publication_metadata(
        self,
        pmid: str,
        summary: Mapping[str, Any] | None,
        *,
        abstract: str | None,
    ) -> PublicationMetadata:
        """Restate the retrieved publication metadata, verbatim."""
        if summary is None:
            return PublicationMetadata(source_identifier=pmid, abstract=abstract)
        return PublicationMetadata(
            source_identifier=pmid,
            title=_optional_str(summary.get("title")),
            abstract=abstract,
            journal=_optional_str(
                summary.get("fulljournalname") or summary.get("source")
            ),
            publication_date=_optional_str(summary.get("pubdate")),
            publication_year=_publication_year(summary.get("pubdate")),
            authors=_name_list(summary.get("authors")),
            publication_types=_string_list(summary.get("pubtype")),
            doi=_doi(summary.get("articleids")),
            language=_first_string(summary.get("lang")),
        )

    def _build_record(
        self,
        query: ConnectorQuery,
        pmid: str,
        metadata: PublicationMetadata,
        *,
        retrieved_at: datetime,
        truncated: bool,
        metadata_missing: bool,
    ) -> EvidenceRecord:
        """Normalize one retrieved publication into a PUBLIC ``EvidenceRecord``.

        The gene is the one that was asked about; disease, phenotype and drug
        restate the query axes. Every field the connector cannot establish from
        the source is left at the schema default and named in ``quality_flags``,
        so a reader is never left to guess which values came from where.
        """
        flags = [FLAG_CONTEXT_FROM_QUERY, FLAG_SPECIES_NOT_ESTABLISHED]
        if truncated:
            flags.append(FLAG_RESULTS_TRUNCATED)
        if metadata_missing:
            flags.append(FLAG_METADATA_UNAVAILABLE)

        return EvidenceRecord(
            evidence_id=f"PUBMED_{pmid}",
            gene=query.gene,
            source_scope=SourceScope.PUBLIC,
            source_type=SOURCE_TYPE,
            source_name=SOURCE_NAME,
            source_identifier=pmid,
            source_url=PUBMED_RECORD_URL.format(pmid=pmid),
            disease=query.disease,
            phenotype=query.phenotype,
            drug=query.drug,
            evidence_level=EvidenceLevel.UNKNOWN,
            support=Support.UNKNOWN,
            directness=Directness.UNASSESSED,
            summary=metadata.title,
            quality_flags=flags,
            retrieved_at=retrieved_at,
        )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _name_list(value: Any) -> list[str]:
    """Author names exactly as reported, in the order reported."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for entry in value:
        if isinstance(entry, dict):
            name = _optional_str(entry.get("name"))
            if name is not None:
                names.append(name)
    return names


def _first_string(value: Any) -> str | None:
    for item in _string_list(value):
        if item.strip():
            return item
    return None


def _doi(article_ids: Any) -> str | None:
    """The DOI the source lists among the article identifiers."""
    if not isinstance(article_ids, list):
        return None
    for entry in article_ids:
        if isinstance(entry, dict) and entry.get("idtype") == "doi":
            doi = _optional_str(entry.get("value"))
            if doi is not None:
                return doi
    return None


def _publication_year(pubdate: Any) -> int | None:
    """Leading year of a source-reported publication date, else ``None``."""
    if not isinstance(pubdate, str):
        return None
    match = _YEAR_PATTERN.match(pubdate.strip())
    return int(match.group()) if match else None


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    text = "".join(node.itertext()).strip()
    return text or None
