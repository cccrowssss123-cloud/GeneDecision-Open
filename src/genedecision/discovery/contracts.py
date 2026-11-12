"""Explicit local declarations; no declaration constitutes scientific validation."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import DatasetManifest

Text = Annotated[str, Field(strict=True, min_length=1)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
MeasurementRole = Literal["MEASUREMENT", "P_VALUE", "ADJUSTED_P_VALUE", "EFFECT"]


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always")

    @field_validator("column", "definition", "namespace", "taxon", "reference_id",
                     "source_namespace", "target_namespace", "source", "target",
                     mode="after", check_fields=False)
    @classmethod
    def nonblank_text(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("DECLARATION_BLANK_TEXT")
        return value


class GeneColumn(Declaration):
    column: Text
    role: Literal["GENE_IDENTIFIER"] = "GENE_IDENTIFIER"
    definition: Text


class MeasurementColumn(Declaration):
    column: Text
    role: MeasurementRole = "MEASUREMENT"
    definition: Text


class LexicalPolicy(Declaration):
    strip_whitespace: Annotated[bool, Field(strict=True)] = False
    case: Literal["PRESERVE", "UPPER", "LOWER"] = "PRESERVE"

    def apply(self, raw: str) -> str:
        value = raw.strip() if self.strip_whitespace else raw
        if self.case == "UPPER":
            return value.upper()
        if self.case == "LOWER":
            return value.lower()
        return value


class MappingBinding(Declaration):
    reference_id: Text
    content_sha256: Digest
    source_namespace: Text
    target_namespace: Text
    taxon: Text


class ColumnContract(Declaration):
    schema_version: Literal["discovery-column-contract-v1"] = "discovery-column-contract-v1"
    declaration_semantics: Literal["USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"] = (
        "USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED"
    )
    primary_gene: GeneColumn
    measurements: Annotated[tuple[MeasurementColumn, ...], Field(min_length=1)]
    namespace: Text
    taxon: Text
    lexical_policy: LexicalPolicy = Field(default_factory=LexicalPolicy)
    # Exact raw-string matching; no implicit NA markers or case folding.
    missing_identifier_tokens: tuple[Annotated[str, Field(strict=True)], ...] = ()
    mapping_reference: MappingBinding | None = None

    @model_validator(mode="after")
    def consistent_selection(self) -> "ColumnContract":
        names = [self.primary_gene.column, *(m.column for m in self.measurements)]
        if len(set(names)) != len(names):
            raise ValueError("CONTRACT_COLUMN_CONFLICT")
        if len(set(self.missing_identifier_tokens)) != len(self.missing_identifier_tokens):
            raise ValueError("CONTRACT_DUPLICATE_MISSING_TOKEN")
        binding = self.mapping_reference
        if binding and (binding.source_namespace != self.namespace or binding.taxon != self.taxon):
            raise ValueError("CONTRACT_REFERENCE_DECLARATION_CONFLICT")
        return self

    @property
    def content_sha256(self) -> str:
        return DatasetManifest.content_digest(self.model_dump(mode="json"))


class MappingEntry(Declaration):
    source: Text
    target: Text


class LocalMappingReference(Declaration):
    """Caller-supplied reference, never downloaded or scientifically certified.

    Keys are matched exactly against the policy-derived identifier. No policy is
    silently applied to reference keys. Repeated entries remain in the digest.
    """

    schema_version: Literal["local-identifier-reference-v1"] = "local-identifier-reference-v1"
    reference_id: Text
    source_namespace: Text
    target_namespace: Text
    taxon: Text
    entries: tuple[MappingEntry, ...]
    content_sha256: Digest

    def identity_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"content_sha256"})

    @model_validator(mode="after")
    def verify_digest(self) -> "LocalMappingReference":
        if self.content_sha256 != DatasetManifest.content_digest(self.identity_payload()):
            raise ValueError("REFERENCE_DIGEST_MISMATCH")
        return self

    @classmethod
    def from_entries(cls, *, reference_id: str, source_namespace: str,
                     target_namespace: str, taxon: str,
                     entries: tuple[MappingEntry, ...]) -> "LocalMappingReference":
        # Validate all text/entries before producing a canonical content digest.
        payload = dict(schema_version="local-identifier-reference-v1",
                       reference_id=reference_id, source_namespace=source_namespace,
                       target_namespace=target_namespace, taxon=taxon,
                       entries=[MappingEntry.model_validate(e).model_dump(mode="json") for e in entries])
        return cls(**payload, content_sha256=DatasetManifest.content_digest(payload))

    @property
    def binding(self) -> MappingBinding:
        return MappingBinding(**self.model_dump(exclude={"schema_version", "entries"}))
