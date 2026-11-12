"""Local manual-model provenance, deliberately separate from API metadata."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ManualBridgeExecutionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    execution_id: str
    case_id: str
    run_index: int = Field(ge=1, le=5)
    attempt_index: int = Field(default=1, ge=1)
    replacement_for: str | None = None
    snapshot_hash: str
    response_artifact_sha256: str
    provider_name: Literal["Codex"] = "Codex"
    execution_mode: Literal["MANUAL_CODEX_BRIDGE"] = "MANUAL_CODEX_BRIDGE"
    transport: Literal["NONE"] = "NONE"
    external_api_call: Literal[False] = False
    transmission_authorization: Literal["NOT_APPLICABLE_TO_MANUAL_BRIDGE"] = "NOT_APPLICABLE_TO_MANUAL_BRIDGE"
    model_id: None = None
    provider_request_id: None = None
    provider_response_id: None = None
    input_tokens: None = None
    output_tokens: None = None
    reasoning_tokens: None = None
    cost: None = None
    api_latency_ms: None = None
    local_validation_latency_ms: int | None = Field(default=None, ge=0)
    prompt_version: str
    prompt_sha256: str
    schema_version: str
    schema_sha256: str

    @model_validator(mode="after")
    def replacement_identity(self) -> "ManualBridgeExecutionMetadata":
        if (self.attempt_index == 1) != (self.replacement_for is None):
            raise ValueError("replacement identity must accompany a later attempt")
        return self
