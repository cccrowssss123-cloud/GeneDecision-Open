"""Explicit exploratory comparisons and scoped caller QC declarations."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from .categories import _deep_validate
from .category_contracts import SourceBinding
from .contracts import Declaration, Digest, Text
from .measurement_contracts import MeasurementPolicy, RawText
from .measurements import _observe
from .models import DatasetManifest, RowLocator


class NumericDeclaration(Declaration):
    @field_validator("predicate_id", "measurement_id", "provenance", "context_id", "release",
                     "record_id", "source_id", "source_locator", "declaration_id", "measurement_audit_id",
                     mode="after", check_fields=False)
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("NUMERIC_DECLARATION_BLANK_TEXT")
        return value


class NumericContentContract(NumericDeclaration):
    content_sha256: Digest

    def identity_payload(self):
        return self.model_dump(mode="json", exclude={"content_sha256"})

    @model_validator(mode="after")
    def verify_digest(self, info: ValidationInfo):
        digest = DatasetManifest.content_digest(self.identity_payload())
        if info.context and info.context.get("create_digest"):
            object.__setattr__(self, "content_sha256", digest)
        elif self.content_sha256 != digest:
            raise ValueError("NUMERIC_DECLARATION_DIGEST_MISMATCH")
        return self

    @classmethod
    def from_declarations(cls, **payload):
        payload["content_sha256"] = "0" * 64
        return cls.model_validate(cls.model_validate(payload, context={"create_digest": True}))


def threshold_value(text):
    # Reuse 6B4A grammar, exact construction and fixed resource limits. Threshold
    # strings do not use missing markers, whitespace stripping or float parsing.
    return _observe(text, MeasurementPolicy(measurement_id="CONSTANT", column="CONSTANT"))


class NumericPredicate(NumericDeclaration):
    predicate_id: Text
    measurement_id: Text
    operator: Literal["LT", "LE", "GT", "GE", "EQ"] | None = None
    threshold: RawText | None = None
    unresolved_reason: Text | None = None
    provenance: Text

    @model_validator(mode="after")
    def explicit_definition(self):
        unresolved = self.operator is None or self.threshold is None
        if unresolved != (self.unresolved_reason is not None):
            raise ValueError("NUMERIC_PREDICATE_DEFINITION_CONFLICT")
        if self.unresolved_reason is not None and not self.unresolved_reason.strip():
            raise ValueError("NUMERIC_PREDICATE_REASON_BLANK")
        if self.threshold is not None and threshold_value(self.threshold).status != "FINITE_DECIMAL":
            raise ValueError("NUMERIC_THRESHOLD_NOT_FINITE_ASCII_DECIMAL")
        return self


class NumericCondition(NumericContentContract):
    schema_version: Literal["discovery-numeric-condition-v1"] = "discovery-numeric-condition-v1"
    semantics_version: Literal["EXPLORATORY_NUMERIC_ISSUE7_V1"] = "EXPLORATORY_NUMERIC_ISSUE7_V1"
    measurement_audit_id: Text
    context_sha256: Digest | None = None
    predicates: Annotated[tuple[NumericPredicate, ...], Field(min_length=1, max_length=128)]
    combine: Literal["ANY", "ALL"]

    @model_validator(mode="after")
    def unique_predicates(self):
        if len({p.predicate_id for p in self.predicates}) != len(self.predicates):
            raise ValueError("NUMERIC_PREDICATE_DUPLICATE_ID")
        return self


class NumericSourceRecord(NumericDeclaration):
    record_id: Text
    source_id: Text
    release: Text
    source_locator: Text
    statement: RawText


class QCDeclaration(NumericDeclaration):
    declaration_id: Text
    status: Literal["UNKNOWN", "DECLARED_PASS", "DECLARED_FAIL"]
    source_binding: SourceBinding
    source_record_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    measurement_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    scope: Literal["DATASET", "ROWS"]
    locators: tuple[RowLocator, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self):
        # Legacy RowLocator instances must not bypass strict int fields, even
        # through direct public model validation with mixed models/dictionaries.
        locators = tuple(_deep_validate(RowLocator, x) for x in self.locators)
        if (self.scope == "ROWS") != bool(locators):
            raise ValueError("NUMERIC_QC_SCOPE_CONFLICT")
        for values in (self.source_record_ids, self.measurement_ids, locators):
            if len(set(values)) != len(values):
                raise ValueError("NUMERIC_QC_SCOPE_DUPLICATE")
        return self


class NumericContext(NumericContentContract):
    schema_version: Literal["discovery-numeric-context-v1"] = "discovery-numeric-context-v1"
    declaration_semantics: Literal["USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"] = "USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"
    context_id: Text
    release: Text
    measurement_audit_id: Text
    source_records: tuple[NumericSourceRecord, ...] = ()
    qc_declarations: tuple[QCDeclaration, ...] = ()
    caller_context_text: RawText | None = None
    tested_universe_text: RawText | None = None

    @model_validator(mode="after")
    def validate_sources(self):
        for values in ([r.record_id for r in self.source_records],
                       [d.declaration_id for d in self.qc_declarations]):
            if len(set(values)) != len(values):
                raise ValueError("NUMERIC_CONTEXT_DUPLICATE_ID")
        records = {r.record_id: r for r in self.source_records}
        for declaration in self.qc_declarations:
            for record_id in declaration.source_record_ids:
                record = records.get(record_id)
                if record is None:
                    raise ValueError("NUMERIC_QC_SOURCE_RECORD_MISSING")
                if (record.source_id, record.release) != (declaration.source_binding.source_id,
                                                         declaration.source_binding.release):
                    raise ValueError("NUMERIC_QC_SOURCE_RELEASE_MISMATCH")
        return self
