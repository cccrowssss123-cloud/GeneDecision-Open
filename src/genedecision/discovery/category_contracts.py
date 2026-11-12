"""Versioned caller declarations, never a certification of scientific truth."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from .contracts import Declaration, Digest, Text
from .models import DatasetManifest


class CategoryDeclaration(Declaration):
    @field_validator("*", mode="after")
    @classmethod
    def nonblank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("CATEGORY_BLANK_TEXT")
        if isinstance(value, tuple) and any(isinstance(v, str) and not v.strip() for v in value):
            raise ValueError("CATEGORY_BLANK_TEXT")
        return value


class ContentContract(CategoryDeclaration):
    content_sha256: Digest

    def identity_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"content_sha256"})

    @model_validator(mode="after")
    def verify_digest(self, info: ValidationInfo):
        digest = DatasetManifest.content_digest(self.identity_payload())
        if info.context and info.context.get("create_digest"):
            # Only the explicit constructor computes a digest; loaders verify it.
            object.__setattr__(self, "content_sha256", digest)
        elif self.content_sha256 != digest:
            raise ValueError("CATEGORY_DIGEST_MISMATCH")
        return self

    @classmethod
    def from_declarations(cls, **payload):
        payload["content_sha256"] = "0" * 64
        created = cls.model_validate(payload, context={"create_digest": True})
        return cls.model_validate(created)


class SourceBinding(CategoryDeclaration):
    source_id: Text
    release: Text


class SourceVersion(SourceBinding):
    description: Text


class AnnotationProvenance(CategoryDeclaration):
    description: Text
    source_versions: Annotated[tuple[SourceVersion, ...], Field(min_length=1)]
    citations: tuple[Text, ...] = ()


class AnnotationLicense(CategoryDeclaration):
    identifier: Text
    notes: Text
    local_use: Literal["ALLOWED", "DISALLOWED", "UNKNOWN"]
    redistribution: Literal["ALLOWED", "DISALLOWED", "UNKNOWN"]


class CategoryDefinition(CategoryDeclaration):
    category_id: Text
    label: Text
    definition: Text
    definition_source: Text


class SourceRecord(CategoryDeclaration):
    record_id: Text
    source_binding: SourceBinding
    source_locator: Text
    notes: Text | None = None


class CategoryAssertion(CategoryDeclaration):
    assertion_id: Text
    category_id: Text
    state: Literal["PRESENT", "ABSENT", "UNCERTAIN"]
    source_binding: SourceBinding
    source_record_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    # For positive/uncertain evidence this is an optional applicability scope.
    # ABSENT requires a matching COMPLETE scope to support FALSE.
    complete_scope_id: Text | None = None


class AnnotationEntity(CategoryDeclaration):
    entity_record_id: Text
    target_identifier: Text
    source_record_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    assertions: tuple[CategoryAssertion, ...]


class CompleteScope(CategoryDeclaration):
    scope_id: Text
    source_binding: SourceBinding
    covered_identifiers: tuple[Text, ...]
    category_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    scope_definition: Text
    source_record_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    coverage_status: Literal["COMPLETE", "PARTIAL", "UNKNOWN"]


class AnnotationBinding(CategoryDeclaration):
    reference_id: Text
    release: Text
    content_sha256: Digest
    namespace: Text
    taxon: Text


def _unique(values):
    if len(set(values)) != len(values):
        raise ValueError("CATEGORY_DUPLICATE_STRUCTURAL_ID")


class AnnotationReference(ContentContract):
    schema_version: Literal["discovery-annotation-reference-v1"] = "discovery-annotation-reference-v1"
    semantics_version: Literal["category-assertion-v1"] = "category-assertion-v1"
    reference_id: Text
    release: Text
    namespace: Text
    taxon: Text
    provenance: AnnotationProvenance
    license: AnnotationLicense
    vocabulary: Annotated[tuple[CategoryDefinition, ...], Field(min_length=1)]
    source_records: Annotated[tuple[SourceRecord, ...], Field(min_length=1)]
    entities: tuple[AnnotationEntity, ...]
    complete_scopes: tuple[CompleteScope, ...]

    @property
    def binding(self) -> AnnotationBinding:
        return AnnotationBinding(**{name: getattr(self, name) for name in AnnotationBinding.model_fields})

    @model_validator(mode="after")
    def validate_graph(self):
        versions = [(s.source_id, s.release) for s in self.provenance.source_versions]
        _unique(versions)
        _unique([v.category_id for v in self.vocabulary])
        _unique([r.record_id for r in self.source_records])
        _unique([e.entity_record_id for e in self.entities])
        _unique([s.scope_id for s in self.complete_scopes])
        _unique([a.assertion_id for e in self.entities for a in e.assertions])
        records = {r.record_id: r for r in self.source_records}
        categories = {v.category_id for v in self.vocabulary}
        scopes = {s.scope_id: s for s in self.complete_scopes}

        def binding_exists(binding):
            if (binding.source_id, binding.release) not in versions:
                raise ValueError("CATEGORY_SOURCE_VERSION_UNDECLARED")

        def check_records(ids, binding=None):
            _unique(ids)
            if any(i not in records for i in ids):
                raise ValueError("CATEGORY_RECORD_REFERENCE_MISSING")
            if binding and any(records[i].source_binding != binding for i in ids):
                raise ValueError("CATEGORY_RECORD_SOURCE_CONFLICT")

        for record in self.source_records:
            binding_exists(record.source_binding)
        for category in self.vocabulary:
            check_records((category.definition_source,))
        # A source/release cannot declare incompatible completeness on the same
        # exact target/category pair. Different sources/releases remain separate.
        coverage_by_source = {}
        for scope in self.complete_scopes:
            binding_exists(scope.source_binding)
            check_records(scope.source_record_ids, scope.source_binding)
            _unique(scope.covered_identifiers)
            _unique(scope.category_ids)
            if not set(scope.category_ids) <= categories:
                raise ValueError("CATEGORY_UNKNOWN_SCOPE_CATEGORY")
            key = (scope.source_binding.source_id, scope.source_binding.release)
            targets, scope_categories = frozenset(scope.covered_identifiers), frozenset(scope.category_ids)
            previous = coverage_by_source.setdefault(key, [])
            for status, previous_targets, previous_categories in previous:
                if (status != scope.coverage_status and not targets.isdisjoint(previous_targets)
                        and not scope_categories.isdisjoint(previous_categories)):
                    raise ValueError("CATEGORY_SCOPE_CONTRADICTION")
            previous.append((scope.coverage_status, targets, scope_categories))
        for entity in self.entities:
            check_records(entity.source_record_ids)
            for assertion in entity.assertions:
                binding_exists(assertion.source_binding)
                check_records(assertion.source_record_ids, assertion.source_binding)
                if assertion.category_id not in categories:
                    raise ValueError("CATEGORY_ASSERTION_CATEGORY_MISSING")
                if assertion.complete_scope_id is not None and assertion.complete_scope_id not in scopes:
                    raise ValueError("CATEGORY_SCOPE_REFERENCE_MISSING")
        return self


class MemberOf(CategoryDeclaration):
    condition_id: Text
    operator: Literal["MEMBER_OF"] = "MEMBER_OF"
    category_id: Text


class CategoryCondition(ContentContract):
    schema_version: Literal["discovery-category-condition-v1"] = "discovery-category-condition-v1"
    semantics_version: Literal["category-membership-three-valued-v1"] = "category-membership-three-valued-v1"
    annotation_binding: AnnotationBinding
    association_mode: Literal["DECLARED_DIRECT_KEY", "BOUND_MAPPING_TARGET"]
    direct_key_policy: Literal["RAW_EXACT"] | None = None
    predicates: Annotated[tuple[MemberOf, ...], Field(min_length=1)]
    combine: Literal["ANY", "ALL"]
    ambiguous_row_policy: Literal["STRICT_UNKNOWN"] = "STRICT_UNKNOWN"
    unknown_policy: Literal["RETAIN_SEPARATELY"] = "RETAIN_SEPARATELY"

    @model_validator(mode="after")
    def explicit_mode(self):
        _unique([p.condition_id for p in self.predicates])
        if (self.association_mode == "DECLARED_DIRECT_KEY") != (self.direct_key_policy == "RAW_EXACT"):
            raise ValueError("CATEGORY_DIRECT_POLICY_CONFLICT")
        return self
