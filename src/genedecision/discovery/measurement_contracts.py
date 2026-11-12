"""Caller-declared lexical audit policies; no scientific measurement judgment."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from .contracts import Declaration, Digest, Text
from .models import DatasetManifest

RawText = Annotated[str, Field(strict=True)]
MAX_TOKEN_CHARACTERS = 4096
MAX_SIGNIFICANT_DIGITS = 1024
MAX_EXPONENT_MAGNITUDE = 10000


class MeasurementParserLimits(Declaration):
    token_characters: Annotated[int, Field(strict=True, ge=1)] = MAX_TOKEN_CHARACTERS
    significant_digits: Annotated[int, Field(strict=True, ge=1)] = MAX_SIGNIFICANT_DIGITS
    exponent_magnitude: Annotated[int, Field(strict=True, ge=1)] = MAX_EXPONENT_MAGNITUDE

    @model_validator(mode="after")
    def fixed_version(self):
        if (self.token_characters, self.significant_digits, self.exponent_magnitude) != (
                MAX_TOKEN_CHARACTERS, MAX_SIGNIFICANT_DIGITS, MAX_EXPONENT_MAGNITUDE):
            raise ValueError("MEASUREMENT_PARSER_LIMITS_VERSION_CONFLICT")
        return self


class MeasurementPolicy(Declaration):
    measurement_id: Text
    column: Text
    missing_token_matching: Literal["RAW_EXACT"] = "RAW_EXACT"
    missing_tokens: tuple[RawText, ...] = ()
    strip_ascii_whitespace: Annotated[bool, Field(strict=True)] = False

    @field_validator("measurement_id")
    @classmethod
    def nonblank_id(cls, value):
        if not value.strip():
            raise ValueError("MEASUREMENT_POLICY_BLANK_ID")
        return value

    @model_validator(mode="after")
    def explicit_tokens(self):
        if any(not t.strip() for t in self.missing_tokens):
            raise ValueError("MEASUREMENT_MISSING_TOKEN_BLANK")
        if len(set(self.missing_tokens)) != len(self.missing_tokens):
            raise ValueError("MEASUREMENT_MISSING_TOKEN_DUPLICATE")
        return self


class MeasurementSourceDeclaration(Declaration):
    """Verbatim metadata only; no source validity, QC or contradiction assessment."""

    record_id: Text
    text: RawText
    source_locator: RawText | None = None

    @field_validator("record_id")
    @classmethod
    def nonblank_id(cls, value):
        if not value.strip():
            raise ValueError("MEASUREMENT_SOURCE_BLANK_ID")
        return value


class MeasurementAuditContract(Declaration):
    schema_version: Literal["discovery-measurement-audit-contract-v1"] = "discovery-measurement-audit-contract-v1"
    declaration_semantics: Literal["USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"] = "USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"
    column_contract_sha256: Digest
    parser_semantics: Literal["ASCII_FINITE_DECIMAL_V1"] = "ASCII_FINITE_DECIMAL_V1"
    # Fixed resource limits are bound by the parser semantics version. They are
    # engineering limits, never numeric thresholds or scientific domains.
    parser_limits: MeasurementParserLimits = Field(default_factory=MeasurementParserLimits)
    policies: Annotated[tuple[MeasurementPolicy, ...], Field(min_length=1)]
    caller_context_text: RawText | None = None
    source_declarations: tuple[MeasurementSourceDeclaration, ...] = ()
    content_sha256: Digest

    def identity_payload(self):
        return self.model_dump(mode="json", exclude={"content_sha256"})

    @model_validator(mode="after")
    def unique_content(self, info: ValidationInfo):
        for values in ([p.measurement_id for p in self.policies],
                       [p.column for p in self.policies],
                       [s.record_id for s in self.source_declarations]):
            if len(set(values)) != len(values):
                raise ValueError("MEASUREMENT_DECLARATION_DUPLICATE_ID")
        digest = DatasetManifest.content_digest(self.identity_payload())
        if info.context and info.context.get("create_digest"):
            object.__setattr__(self, "content_sha256", digest)
        elif self.content_sha256 != digest:
            raise ValueError("MEASUREMENT_CONTRACT_DIGEST_MISMATCH")
        return self

    @classmethod
    def from_declarations(cls, **payload):
        payload["content_sha256"] = "0" * 64
        created = cls.model_validate(payload, context={"create_digest": True})
        return cls.model_validate(created)
