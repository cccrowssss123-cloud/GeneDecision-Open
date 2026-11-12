"""Receipt recording and bounded serial HTTP policy for the existing connector.

This adapter never parses publications, writes a database, or issues novelty
judgments. Simulated transports must be explicitly labeled as simulated.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit
from uuid import uuid4

from genedecision.connectors.base import ConnectorRunResult, ConnectorStatus, FLAG_METADATA_UNAVAILABLE
from genedecision.connectors.transport import HttpTransport, TransportError, TransportResponse
from genedecision.connectors.pubmed_collection_http import HeaderAwareUrllibTransport
from genedecision.connectors.pubmed import PubMedConnector


def caller_email(email: str | None = None) -> str:
    """Validate syntax only; never invent or verify ownership of an identity."""
    value = email if email is not None else (
        os.environ.get('GENE_DECISION_NCBI_EMAIL') or os.environ.get('NCBI_EMAIL') or ''
    )
    value = value.strip()
    pattern = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
    local = value.split('@', 1)[0]
    if (not re.fullmatch(pattern, value) or local.startswith('.')
        or local.endswith('.') or '..' in local):
        raise ValueError('valid NCBI caller email required: email=, GENE_DECISION_NCBI_EMAIL or NCBI_EMAIL')
    return value


class PubMedCollectionTransport:
    """Wrap the real transport; preserve every attempted request before retrying."""
    version = 'pubmed-collection-transport-v1'

    def __init__(
        self, upstream: HttpTransport, *, receipt_directory: Path,
        declared_scope: str, connector_identity: Mapping[str, str],
        simulated: bool = False, min_interval: float = 1.0, max_retries: int = 2,
        max_backoff: float = 30.0, monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not declared_scope.strip():
            raise ValueError('a declared collection scope is required')
        if (not math.isfinite(min_interval) or min_interval < 1 / 3
            or type(max_retries) is not int or not 0 <= max_retries <= 3
            or not math.isfinite(max_backoff) or not 1 <= max_backoff <= 30):
            raise ValueError('invalid bounded pacing/retry configuration')
        self.upstream = upstream
        self.declared_scope = declared_scope
        self.connector_identity = dict(connector_identity)
        self.simulated = simulated
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.max_backoff = max_backoff
        self._monotonic, self._sleep, self._clock = monotonic, sleep, clock
        self._next_start = 0.0
        self._lock = threading.Lock()
        self._attempts: list[dict] = []
        self._secrets: set[str] = set()
        self.directory = Path(receipt_directory) / f'pubmed_{uuid4().hex}'
        self.directory.mkdir(parents=True, exist_ok=False)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError('receipt clock must be timezone-aware')
        return value.astimezone(timezone.utc)

    def _redact(self, text: str) -> str:
        variants = {v for secret in self._secrets for v in (secret, quote(secret, safe=''), quote_plus(secret))}
        for secret in sorted(variants, key=len, reverse=True):
            if secret:
                text = text.replace(secret, '[REDACTED]')
        return text

    def _retry_delay(self, response: TransportResponse, attempt: int) -> tuple[float | None, str]:
        if not (response.status_code == 429 or 500 <= response.status_code <= 599):
            return None, 'NOT_RETRYABLE'
        if attempt >= self.max_retries:
            return None, 'RETRY_BOUND_EXHAUSTED'
        header = next((v for k, v in (getattr(response, 'headers', None) or {}).items() if k.lower() == 'retry-after'), None)
        delay = None
        if header is not None:
            try:
                if header.strip().isdigit():
                    delay = float(header.strip())
                else:
                    date = parsedate_to_datetime(header)
                    if date.tzinfo is not None:
                        delay = max(0.0, (date - self._now()).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
        if delay is not None and (not math.isfinite(delay) or delay > self.max_backoff):
            # Do not retry earlier than the server asked just to fit our bound.
            return None, 'RETRY_AFTER_EXCEEDS_WAIT_BOUND'
        return max(min(2.0 ** attempt, self.max_backoff), delay or 0.0), 'BOUNDED_RETRY'

    def _record(self, data: dict, body: str | None = None) -> None:
        ordinal = len(self._attempts) + 1
        stem = f'attempt_{ordinal:04d}'
        data.update(
            schema_version='pubmed-request-receipt-v1', declared_scope=self.declared_scope,
            execution_mode='SIMULATED_HTTP' if self.simulated else 'REAL_HTTP',
            source_content_independently_verified=False,
            connector_identity=self.connector_identity, transport_version=self.version,
            request_ordinal=ordinal, min_interval_seconds=self.min_interval,
            max_retries=self.max_retries, max_backoff_seconds=self.max_backoff,
        )
        if body is not None:
            original = body.encode('utf-8')
            stored = self._redact(body).encode('utf-8')
            path = self.directory / f'{stem}.body.txt'
            with path.open('xb') as stream:
                stream.write(stored)
            data.update(
                response_body_sha256=hashlib.sha256(original).hexdigest(),
                stored_body_sha256=hashlib.sha256(stored).hexdigest(),
                body_path=path.name, body_encoding='decoded UTF-8 text; not original wire bytes',
                body_redacted=stored != original,
            )
        else:
            data.update(response_body_sha256=None, stored_body_sha256=None, body_path=None)
        path = self.directory / f'{stem}.json'
        with path.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
        self._attempts.append({**data, 'receipt_path': path.name,
            'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

    def get(self, url: str, params: Mapping[str, str], *, timeout: float | None = None) -> TransportResponse:
        endpoint = urlsplit(url)
        if (endpoint.scheme != 'https' or endpoint.netloc != 'eutils.ncbi.nlm.nih.gov'
            or endpoint.query or endpoint.fragment or endpoint.path not in {
                '/entrez/eutils/esearch.fcgi', '/entrez/eutils/esummary.fcgi', '/entrez/eutils/efetch.fcgi'}):
            raise ValueError('collection transport requires an official PubMed E-utilities endpoint')
        caller_email(params.get('email', ''))
        if params.get('db') != 'pubmed' or not params.get('tool', '').strip():
            raise ValueError('PubMed database and caller tool identity required')
        # One lock covers every attempt and backoff, even if callers share it.
        with self._lock:
            self._secrets.update(str(params[k]) for k in ('email', 'api_key') if params.get(k))
            submitted = {k: self._redact(str(v)) for k, v in params.items() if k not in ('email', 'api_key')}
            for attempt in range(self.max_retries + 1):
                while (wait := self._next_start - self._monotonic()) > 0:
                    self._sleep(wait)
                started = self._now()
                self._next_start = self._monotonic() + self.min_interval
                data = dict(endpoint=url, operation=endpoint.path.rsplit('/', 1)[-1],
                    submitted_parameters=submitted, request_started_at=started.isoformat(),
                    timeout_seconds=timeout, attempt_index=attempt + 1,
                    caller_email_configured=True, api_key_configured=bool(params.get('api_key')))
                try:
                    response = self.upstream.get(url, params, timeout=timeout)
                except TransportError:
                    self._record({**data, 'request_completed_at': self._now().isoformat(),
                        'http_status': None, 'request_status': 'TRANSPORT_FAILURE',
                        'retry_decision': 'TRANSPORT_FAILURE_NOT_RETRIED'})
                    raise TransportError('PubMed transport failure; see local request receipt') from None
                delay, reason = self._retry_delay(response, attempt)
                self._record({**data, 'request_completed_at': self._now().isoformat(),
                    'http_status': response.status_code, 'request_status': 'HTTP_RESPONSE',
                    'retry_after': self._redact(next((v for k, v in (getattr(response, 'headers', None) or {}).items()
                        if k.lower() == 'retry-after'), '')) or None,
                    'retry_decision': reason, 'retry_delay_seconds': delay}, response.body)
                if delay is None:
                    return response
                self._next_start = max(self._next_start, self._monotonic() + delay)
        raise AssertionError('bounded retry loop exhausted without returning')

    def write_run_receipt(self, run: ConnectorRunResult) -> Path:
        """Keep parser outcomes separate from HTTP success and from coverage."""
        if run.connector != 'PubMed':
            raise ValueError('receipt requires a PubMed connector run')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', run.run_id):
            raise ValueError('receipt requires a safe run identifier')
        if run.retrieved_at.tzinfo is None:
            raise ValueError('run receipt timestamp must be timezone-aware')
        submitted = {a['submitted_parameters'].get('term') for a in self._attempts
                     if a['operation'] == 'esearch.fcgi'}
        if any(r.query_string not in submitted for r in run.query_results if r.query_string):
            raise ValueError('run queries are not bound to this transport session')
        path = self.directory / f'run_{run.run_id}.json'
        # The source controls titles/abstracts; never persist echoed credentials.
        raw_result = run.model_dump_json(indent=2)
        stored_result = self._redact(raw_result)
        with path.open('x', encoding='utf-8') as stream:
            stream.write(stored_result)
        complete = all(r.status is not ConnectorStatus.FAILURE and not r.truncated
            and r.total_matches is not None
            and r.total_matches == r.returned_count
            and not any(FLAG_METADATA_UNAVAILABLE in e.quality_flags for e in r.records)
            for r in run.query_results)
        manifest = dict(schema_version='pubmed-run-receipt-v1', run_id=run.run_id,
            declared_scope=self.declared_scope, retrieved_at=run.retrieved_at.astimezone(timezone.utc).isoformat(),
            execution_mode='SIMULATED_HTTP' if self.simulated else 'REAL_HTTP',
            connector_identity=self.connector_identity, attempts=list(self._attempts),
            aggregate_status=run.status.value, bounded_query_retrieval_complete=complete,
            partial_results=bool(run.records) and not complete,
            query_outcomes=[dict(query=self._redact(r.query_string) if r.query_string else None, status=r.status.value,
                total_matches=r.total_matches, returned_pmids=[e.source_identifier for e in r.records],
                truncated=r.truncated, failure_kind=r.error.kind.value if r.error else None)
                for r in run.query_results],
            returned_pmids=[e.source_identifier for e in run.records],
            result_path=path.name, result_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            result_redacted=stored_result != raw_result, novelty='UNKNOWN',
            novelty_search_provenance=[], coverage_audit_created=False,
            source_content_independently_verified=False)
        receipt = self.directory / f'run_{run.run_id}.receipt.json'
        with receipt.open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
        return receipt


class PreparedPubMedCollection:
    """Operational session around the unchanged connector, not a new parser."""
    def __init__(self, connector: PubMedConnector, transport: PubMedCollectionTransport):
        self.connector = connector
        self.transport = transport

    def write_receipt(self, run: ConnectorRunResult) -> Path:
        return self.transport.write_run_receipt(run)


def prepare_pubmed_collection(
    *, receipt_directory: Path, declared_scope: str, email: str | None = None,
    api_key: str | None = None, tool: str = 'GeneDecision', retmax: int = 20,
    sort: str | None = 'relevance', timeout: float = 15.0,
    upstream: HttpTransport | None = None, simulated: bool = False,
    **transport_policy,
) -> PreparedPubMedCollection:
    """Validate identity and configure collection; no HTTP or database writes.

    The explicitly simulated seam exists only for offline testing. Actual live
    construction always uses the urllib header extension; callers cannot label
    a supplied fixture transport as real source retrieval.
    """
    identity = caller_email(email)
    if upstream is not None and not simulated:
        raise ValueError('injected collection upstream must be labeled simulated')
    if type(retmax) is not int or not 1 <= retmax <= 100 or not tool.strip():
        raise ValueError('bounded retmax (1..100) and nonempty caller tool required')
    if not 0 < timeout <= 60:
        raise ValueError('collection timeout must be bounded (0..60 seconds)')
    if simulated and upstream is None:
        raise ValueError('simulated collection requires an injected upstream')
    adapter = PubMedCollectionTransport(
        upstream if upstream is not None else HeaderAwareUrllibTransport(timeout=timeout),
        receipt_directory=receipt_directory, declared_scope=declared_scope,
        connector_identity={'name': 'PubMed', 'module': PubMedConnector.__module__,
            'implementation_sha256': hashlib.sha256(Path(inspect.getfile(PubMedConnector)).read_bytes()).hexdigest(),
            'recorder_implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'http_extension_implementation_sha256': hashlib.sha256(Path(inspect.getfile(HeaderAwareUrllibTransport)).read_bytes()).hexdigest()},
        simulated=simulated, **transport_policy,
    )
    connector = PubMedConnector(transport=adapter, email=identity, api_key=api_key,
        tool=tool, retmax=retmax, sort=sort, timeout=timeout)
    return PreparedPubMedCollection(connector, adapter)
