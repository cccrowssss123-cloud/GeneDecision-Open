"""Optional OpenAI Responses transport for bounded LLM interpretation.

The official SDK is imported only when an actual client is needed. Tests inject
an offline client and inspect the exact one-call request.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit

from genedecision.interpretation.llm import LLMAdapterResult, LLMProviderError
from genedecision.models.interpretation import ProviderUsage


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


class OpenAIResponsesAdapter:
    """One stateless, tool-free Responses call with SDK retries disabled."""

    provider_name = "OpenAI"
    adapter_name = "openai-responses"
    adapter_version = "1"

    def __init__(
        self, *, client: Any = None, model_id: str = "gpt-6-sol",
        reasoning_effort: str = "high",
        data_destination: str = "https://api.openai.com/v1/responses",
    ) -> None:
        destination = urlsplit(data_destination)
        if (destination.scheme != "https" or not destination.netloc
            or destination.username or destination.password or destination.query
            or destination.fragment or not destination.path.endswith("/responses")):
            raise ValueError("data destination must be a secret-free HTTPS Responses endpoint")
        self._client = client
        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self.data_destination = data_destination
        self.execution_configuration = {
            "api_style": "Responses",
            "reasoning_effort": reasoning_effort,
            "structured_output": "strict_json_schema",
            "store": False,
            "tools_enabled": False,
            "sdk_max_retries": 0,
            "follow_redirects": False,
        }

    def validated_destination(self) -> str:
        """Reject a client whose effective endpoint differs from consent."""
        if self._client is not None:
            self._assert_client_destination(self._client)
        return self.data_destination

    def _assert_client_destination(self, client: Any) -> None:
        try:
            base_url = str(client.base_url)
            parsed = urlsplit(base_url)
            endpoint = base_url.rstrip("/") + "/responses"
            if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.query or parsed.fragment or endpoint != self.data_destination):
                raise ValueError("mismatch")
        except Exception:
            raise LLMProviderError("provider destination mismatch") from None
        transport = getattr(client, "_client", None)
        if transport is not None and getattr(transport, "follow_redirects", None) is not False:
            raise LLMProviderError("provider redirects enabled")

    def _client_without_retries(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI  # optional, provider-local dependency
                import httpx
            except ImportError:
                raise LLMProviderError("provider dependency unavailable") from None
            try:
                options: dict[str, Any] = {
                    "max_retries": 0,
                    "base_url": self.data_destination.removesuffix("/responses"),
                    "http_client": httpx.Client(follow_redirects=False, trust_env=False),
                }
                self._client = OpenAI(**options)
            except Exception:
                raise LLMProviderError("provider authentication/configuration failed") from None
        self._assert_client_destination(self._client)
        try:
            client = self._client.with_options(max_retries=0)
        except Exception:
            raise LLMProviderError("provider client cannot disable retries") from None
        self._assert_client_destination(client)
        return client

    def execute(
        self, *, snapshot: str, instructions: str, schema: dict[str, Any],
        on_attempt: Callable[[], None],
    ) -> LLMAdapterResult:
        client = self._client_without_retries()
        request = {
            "model": self.model_id,
            "reasoning": {"effort": self.reasoning_effort},
            "instructions": instructions,
            "input": snapshot,
            "text": {"format": {
                "type": "json_schema",
                "name": "llm_interpretation_candidate_v0",
                "strict": True,
                "schema": schema,
            }},
            "store": False,
        }
        # The request deliberately has no tools, conversation, or prior response.
        on_attempt()
        try:
            response = client.responses.create(**request)
        except Exception as exc:
            name = type(exc).__name__
            category = {
                "AuthenticationError": "provider authentication failed",
                "PermissionDeniedError": "provider authentication failed",
                "APITimeoutError": "provider timeout",
                "RateLimitError": "provider rate limited",
                "APIConnectionError": "provider unavailable",
                "InternalServerError": "provider unavailable",
            }.get(name)
            if category is None:
                category = (
                    "provider request too large" if _get(exc, "status_code") == 413
                    else "provider request failed"
                )
            raise LLMProviderError(category) from None

        raw_usage = _get(response, "usage")
        usage = None
        if raw_usage is not None:
            input_units = _get(raw_usage, "input_tokens")
            output_units = _get(raw_usage, "output_tokens")
            billable_units = _get(raw_usage, "total_tokens")
            reasoning_units = _get(_get(raw_usage, "output_tokens_details"), "reasoning_tokens")
            if any(x is not None for x in (input_units, output_units, billable_units,
                                           reasoning_units)):
                usage = ProviderUsage(
                    input_units=input_units, output_units=output_units,
                    billable_units=billable_units, unit_type="tokens",
                    reasoning_units=reasoning_units,
                )

        refusal = False
        unexpected_tool_call = False
        for item in _get(response, "output", ()) or ():
            kind = _get(item, "type")
            if kind not in ("message", "reasoning"):
                unexpected_tool_call = True
            for content in _get(item, "content", ()) or ():
                if _get(content, "type") == "refusal":
                    refusal = True
        return LLMAdapterResult(
            output_text=_get(response, "output_text") or "",
            response_id=_get(response, "id"),
            provider_reported_model=_get(response, "model"),
            usage=usage,
            status=_get(response, "status") or "missing",
            refusal=refusal,
            unexpected_tool_call=unexpected_tool_call,
        )
