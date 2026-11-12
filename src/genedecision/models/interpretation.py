"""Provider-neutral interpretation contracts; no biomedical inference lives here."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from genedecision.models.evidence import EvidenceRecord


class ProviderType(StrEnum):
    HUMAN = "HUMAN"
    RULE_BASED = "RULE_BASED"
    LLM = "LLM"
    JEV = "JEV"
    MOCK = "MOCK"


class InterpretationStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class InterpretedSupport(StrEnum):
    SUPPORT = "SUPPORT"
    CONTRADICT = "CONTRADICT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class InterpretedDirectness(StrEnum):
    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    UNKNOWN = "UNKNOWN"


class DesignType(StrEnum):
    PERTURBATIONAL = "PERTURBATIONAL"
    OBSERVATIONAL = "OBSERVATIONAL"
    ASSOCIATIONAL = "ASSOCIATIONAL"
    UNKNOWN = "UNKNOWN"


class MechanisticRelevance(StrEnum):
    MECHANISTIC = "MECHANISTIC"
    NON_MECHANISTIC = "NON_MECHANISTIC"
    UNKNOWN = "UNKNOWN"


class HypothesisOrigin(StrEnum):
    USER_AUTHORED = "USER_AUTHORED"
    GENERATED_AND_CONFIRMED = "GENERATED_AND_CONFIRMED"


def hypothesis_digest(project_name: str, exact_text: str) -> str:
    """Stable identity for exact confirmed text within one project."""
    payload = json.dumps(
        {"version": 1, "project_name": project_name, "exact_text": exact_text},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str
    project_name: str = Field(min_length=1)
    exact_text: str = Field(min_length=1)
    text_hash: str
    origin: HypothesisOrigin
    confirmed_at: datetime

    @field_validator("exact_text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("hypothesis text must not be blank")
        return value

    @model_validator(mode="after")
    def deterministic_identity(self) -> "Hypothesis":
        digest = hypothesis_digest(self.project_name, self.exact_text)
        if self.text_hash != digest or self.hypothesis_id != f"HYP_{digest}":
            raise ValueError("hypothesis identity/hash does not match confirmed content")
        return self

    @classmethod
    def confirm(
        cls,
        *,
        project_name: str,
        exact_text: str,
        confirmed_at: datetime,
        origin: HypothesisOrigin = HypothesisOrigin.USER_AUTHORED,
    ) -> "Hypothesis":
        digest = hypothesis_digest(project_name, exact_text)
        return cls(
            hypothesis_id=f"HYP_{digest}",
            project_name=project_name,
            exact_text=exact_text,
            text_hash=digest,
            origin=origin,
            confirmed_at=confirmed_at,
        )


class HypothesisDraft(BaseModel):
    """Unconfirmed staging input; never accepted by InterpretationRequest."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    project_name: str = Field(min_length=1)
    proposed_text: str = Field(min_length=1)
    origin: Literal["USER_AUTHORED", "GENERATED"]


class ProjectInterpretationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_name: str
    gene: str
    disease: str | None
    phenotype: str | None
    treatment: str | None
    organism: str | None
    tissue: str | None
    research_question: str


class SourceRecord(BaseModel):
    """A source-specific record and the exact fields shown to the provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    record_type: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    fields: dict[str, Any]


class SourceMetadataView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    records: tuple[SourceRecord, ...] = ()

    @model_validator(mode="after")
    def unique_records(self) -> "SourceMetadataView":
        keys = [(item.record_type, item.record_id) for item in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("source metadata record identities must be unique")
        return self


class SourceFieldReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    record_type: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    field_name: str = Field(min_length=1)
    quote: str | None = None
    text_start: int | None = Field(default=None, ge=0)
    text_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def offset_order(self) -> "SourceFieldReference":
        if self.text_end is not None and (
            self.text_start is None or self.text_end < self.text_start
        ):
            raise ValueError("text_end requires text_start <= text_end")
        return self


class InterpretationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_context: ProjectInterpretationContext
    hypothesis: Hypothesis
    evidence_record: EvidenceRecord
    source_metadata: SourceMetadataView


class InterpretationAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    support: InterpretedSupport
    directness: InterpretedDirectness
    design_type: DesignType
    mechanistic_relevance: MechanisticRelevance
    rationale: str = Field(min_length=1)
    rationale_references: tuple[SourceFieldReference, ...] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("rationale")
    @classmethod
    def nonblank_rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rationale must not be blank")
        return value


class LLMInterpretationCandidate(InterpretationAssessment):
    """Runtime-only scientific output; no run or persistence identity."""

    confidence: Literal[None] = None


class ExternalTransmissionAuthorization(BaseModel):
    """User approval for one exact local snapshot and external destination."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)  # v0 project identity is project_name
    input_snapshot: str
    snapshot_hash: str
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    data_destination: str = Field(min_length=1)
    authorized_at: datetime
    scope: Literal["SINGLE_SNAPSHOT"] = "SINGLE_SNAPSHOT"

    @model_validator(mode="after")
    def snapshot_identity(self) -> "ExternalTransmissionAuthorization":
        if hashlib.sha256(self.input_snapshot.encode("utf-8")).hexdigest() != self.snapshot_hash:
            raise ValueError("authorization snapshot hash does not match content")
        json.loads(self.input_snapshot)
        return self


class ExternalTransmissionPreparation(BaseModel):
    """Read-only preview to show before an explicit authorization action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    input_snapshot: str
    snapshot_hash: str
    provider: str
    model: str
    data_destination: str

    @model_validator(mode="after")
    def valid_snapshot(self) -> "ExternalTransmissionPreparation":
        if hashlib.sha256(self.input_snapshot.encode("utf-8")).hexdigest() != self.snapshot_hash:
            raise ValueError("prepared snapshot hash does not match content")
        return self


class ProviderExecutionMetadata(BaseModel):
    """Operational facts for one external run, separate from interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    adapter_name: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    provider_response_id: str | None = None
    prompt_template_version: str = Field(min_length=1)
    prompt_template_hash: str = Field(min_length=1)
    output_schema_version: str = Field(min_length=1)
    output_schema_hash: str = Field(min_length=1)
    provider_execution_configuration: dict[str, Any]
    data_destination: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    attempt_count: int = Field(ge=0, le=1)
    provider_reported_model: str | None = None

    @field_validator("provider_execution_configuration")
    @classmethod
    def no_secret_configuration(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = ("key", "secret", "token", "authorization", "header", "password", "credential")
        def check(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if any(word in key.lower() for word in forbidden):
                        raise ValueError("execution configuration contains a forbidden key")
                    check(nested)
            elif isinstance(item, list):
                for nested in item:
                    check(nested)
            elif isinstance(item, str) and (
                item.lower().startswith("sk-") or "bearer " in item.lower()
            ):
                raise ValueError("execution configuration contains a secret-like value")
        check(value)
        json.dumps(value, allow_nan=False)
        return value


class ProviderUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_units: float | None = Field(default=None, ge=0)
    output_units: float | None = Field(default=None, ge=0)
    billable_units: float | None = Field(default=None, ge=0)
    unit_type: str | None = None
    currency: str | None = None
    estimated_cost_micros: int | None = Field(default=None, ge=0)
    pricing_version: str | None = None
    reasoning_units: float | None = Field(default=None, ge=0)

    @field_validator("input_units", "output_units", "billable_units", "reasoning_units")
    @classmethod
    def finite_units(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("provider usage units must be finite")
        return value


class InterpretationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    project_name: str
    hypothesis_id: str
    evidence_id: str
    provider_type: ProviderType
    provider_name: str
    provider_version: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    rule_version: str | None = None
    reviewer_id: str | None = None
    input_snapshot: str
    input_snapshot_hash: str
    snapshot_schema_version: str
    snapshot_serialization_version: str
    snapshot_hash_algorithm: str
    started_at: datetime
    completed_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    provider_usage: ProviderUsage | None = None
    status: InterpretationStatus
    error: str | None = None

    @model_validator(mode="after")
    def valid_lifecycle_and_snapshot(self) -> "InterpretationRun":
        if self.snapshot_hash_algorithm != "SHA-256":
            raise ValueError("unsupported snapshot hash algorithm")
        digest = hashlib.sha256(self.input_snapshot.encode("utf-8")).hexdigest()
        if digest != self.input_snapshot_hash:
            raise ValueError("input snapshot hash does not match stored content")
        try:
            json.loads(self.input_snapshot)
        except json.JSONDecodeError as exc:
            raise ValueError("input snapshot must be valid JSON") from exc
        if self.status is InterpretationStatus.STARTED:
            if self.completed_at is not None or self.error is not None:
                raise ValueError("STARTED run cannot have completion or error")
        elif self.status is InterpretationStatus.SUCCEEDED:
            if self.completed_at is None or self.error is not None:
                raise ValueError("SUCCEEDED run needs completion without error")
        elif self.completed_at is None or not self.error:
            raise ValueError("FAILED run needs completion and sanitized error")
        return self


class EvidenceInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    interpretation_id: str
    run_id: str
    project_name: str
    hypothesis_id: str
    evidence_id: str
    support: InterpretedSupport
    directness: InterpretedDirectness
    design_type: DesignType
    mechanistic_relevance: MechanisticRelevance
    rationale: str
    rationale_references: tuple[SourceFieldReference, ...] = Field(min_length=1)
    created_at: datetime
    confidence: float | None = Field(default=None, ge=0, le=1)


def validate_rationale_references(
    request: InterpretationRequest,
    references: tuple[SourceFieldReference, ...],
) -> None:
    """Require every cited record/field to be in this one provider input."""
    allowed: set[tuple[str, str, str]] = {
        ("EvidenceRecord", request.evidence_record.evidence_id, field)
        for field in EvidenceRecord.model_fields
    }
    for record in request.source_metadata.records:
        allowed.update(
            (record.record_type, record.record_id, field) for field in record.fields
        )
    for reference in references:
        key = (reference.record_type, reference.record_id, reference.field_name)
        if key not in allowed:
            raise ValueError("rationale reference is absent from provider input")
        if reference.quote is not None:
            if reference.record_type == "EvidenceRecord":
                value = getattr(request.evidence_record, reference.field_name)
            else:
                source = next(
                    record
                    for record in request.source_metadata.records
                    if (record.record_type, record.record_id)
                    == (reference.record_type, reference.record_id)
                )
                value = source.fields[reference.field_name]
            if not isinstance(value, str) or reference.quote not in value:
                raise ValueError("rationale quote is absent from cited source field")
        if reference.text_start is not None:
            if reference.record_type == "EvidenceRecord":
                value = getattr(request.evidence_record, reference.field_name)
            else:
                source = next(
                    record
                    for record in request.source_metadata.records
                    if (record.record_type, record.record_id)
                    == (reference.record_type, reference.record_id)
                )
                value = source.fields[reference.field_name]
            if not isinstance(value, str) or reference.text_start > len(value):
                raise ValueError("rationale offset is outside cited source field")
            if reference.text_end is not None and reference.text_end > len(value):
                raise ValueError("rationale offset is outside cited source field")
            if reference.quote is not None and reference.text_end is not None:
                if value[reference.text_start:reference.text_end] != reference.quote:
                    raise ValueError("rationale quote does not match cited offsets")
