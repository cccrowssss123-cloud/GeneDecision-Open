"""The single network seam used by evidence connectors.

Connectors never call ``urllib`` directly: they are handed an
:class:`HttpTransport`, so a unit test can inject canned responses and the
default test suite never touches the internet.

The transport reports *what the server said*. It performs no interpretation:
a non-2xx status is returned as a :class:`TransportResponse` for the connector
to classify, while :class:`TransportError` is reserved for the case where no
HTTP response was produced at all (DNS failure, refused connection, TLS error,
timeout, undecodable body).
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import NamedTuple, Protocol, runtime_checkable

DEFAULT_USER_AGENT = "GeneDecision/0.1 (+https://pubmed.ncbi.nlm.nih.gov/)"


class TransportError(RuntimeError):
    """Raised when a request produced no usable HTTP response."""


class TransportResponse(NamedTuple):
    """A raw HTTP response: the status code, the decoded body, and the URL."""

    status_code: int
    body: str
    url: str


@runtime_checkable
class HttpTransport(Protocol):
    """Minimal transport contract: one parameterized GET."""

    def get(
        self,
        url: str,
        params: Mapping[str, str],
        *,
        timeout: float | None = None,
    ) -> TransportResponse:
        """Perform a GET and return the raw response without interpreting it."""
        ...


class UrllibTransport:
    """``HttpTransport`` implemented with the standard library only.

    No third-party HTTP client is required, so the package keeps its single
    runtime dependency (``pydantic``).
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 15.0,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout

    @property
    def timeout(self) -> float:
        return self._timeout

    def get(
        self,
        url: str,
        params: Mapping[str, str],
        *,
        timeout: float | None = None,
    ) -> TransportResponse:
        query = urllib.parse.urlencode({k: v for k, v in params.items()})
        full_url = f"{url}?{query}" if query else url
        request = urllib.request.Request(
            full_url,
            headers={"User-Agent": self._user_agent},
            method="GET",
        )
        effective_timeout = self._timeout if timeout is None else timeout

        try:
            with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                body = response.read().decode("utf-8")
                return TransportResponse(
                    status_code=int(response.status),
                    body=body,
                    url=full_url,
                )
        except urllib.error.HTTPError as exc:
            # An HTTP error status is still a response: the server answered and
            # the connector decides what that answer means.
            try:
                body = exc.read().decode("utf-8")
            except (UnicodeDecodeError, OSError):
                body = ""
            return TransportResponse(
                status_code=int(exc.code),
                body=body,
                url=full_url,
            )
        except UnicodeDecodeError as exc:
            raise TransportError(
                f"response body from {url} was not valid UTF-8"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"request to {url} failed: {exc}") from exc
