"""Header-aware transport extension; preserves the frozen urllib contract."""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

from genedecision.connectors.transport import TransportError, TransportResponse, UrllibTransport


class NoCollectionRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a separate request that must not escape pacing/receipts."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HeaderTransportResponse(TransportResponse):
    """The existing three-field response with optional header metadata."""
    def __new__(cls, status_code: int, body: str, url: str,
                headers: Mapping[str, str] | None = None):
        instance = super().__new__(cls, status_code, body, url)
        instance.headers = dict(headers or {})
        return instance

    def _replace(self, **kwargs):
        headers = kwargs.pop('headers', self.headers)
        return type(self)(**(self._asdict() | kwargs), headers=headers)


class HeaderAwareUrllibTransport(UrllibTransport):
    """Reuse urllib's request/error conventions while exposing Retry-After.

    The frozen transport discards headers and offers no opener seam. This small
    extension keeps its request encoding, user agent, timeout and error contract;
    it adds no publication parser or alternative retrieval endpoint.
    """
    def get(self, url: str, params: Mapping[str, str], *,
            timeout: float | None = None) -> HeaderTransportResponse:
        query = urllib.parse.urlencode({k: v for k, v in params.items()})
        full_url = f'{url}?{query}' if query else url
        request = urllib.request.Request(full_url, headers={'User-Agent': self._user_agent}, method='GET')
        effective_timeout = self._timeout if timeout is None else timeout
        try:
            opener = urllib.request.build_opener(NoCollectionRedirects())
            with opener.open(request, timeout=effective_timeout) as response:
                return HeaderTransportResponse(int(response.status), response.read().decode('utf-8'),
                    full_url, dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode('utf-8')
            except (UnicodeDecodeError, OSError):
                body = ''
            return HeaderTransportResponse(int(exc.code), body, full_url,
                dict(exc.headers.items()) if exc.headers else {})
        except UnicodeDecodeError as exc:
            raise TransportError('response was not valid UTF-8') from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Avoid copying potentially credential-bearing URLs from exceptions.
            raise TransportError('request failed before a usable HTTP response') from exc
