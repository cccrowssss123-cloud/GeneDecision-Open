"""Interpretation orchestration and deterministic provider input snapshots."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel

from genedecision.models.evidence import Directness, EvidenceRecord, SourceScope, Support
from genedecision.models.interpretation import (
    EvidenceInterpretation,
    ExternalTransmissionAuthorization,
    ExternalTransmissionPreparation,
    Hypothesis,
    HypothesisDraft,
    InterpretationAssessment,
    InterpretationRequest,
    InterpretationRun,
    InterpretationStatus,
    ProviderType,
    ProviderUsage,
    ProviderExecutionMetadata,
    ProjectInterpretationContext,
    SourceMetadataView,
    validate_rationale_references,
)
from genedecision.storage import EvidenceDatabase

SNAPSHOT_SCHEMA_VERSION = "interpretation-input-v1"
SNAPSHOT_SERIALIZATION_VERSION = "canonical-json-v1"
SNAPSHOT_HASH_ALGORITHM = "SHA-256"
SAFE_PROVIDER_FAILURES = frozenset({
    "provider dependency unavailable", "provider authentication/configuration failed",
    "provider client cannot disable retries", "provider authentication failed",
    "provider timeout", "provider rate limited", "provider unavailable",
    "provider request too large", "provider request failed", "provider refusal",
    "provider destination mismatch",
    "provider redirects enabled",
    "provider response incomplete", "provider returned a forbidden tool call",
    "provider output invalid", "provider execution failed",
    "provider attempted more than one external call",
    "provider made no recorded external attempt",
})


def _canonical(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def canonical_input_snapshot(request: InterpretationRequest) -> tuple[str, str]:
    """Return the exact frozen provider input and a stable SHA-256 digest.

    Source records are a set of identified facts, so their order does not
    change the logical input. All other list order remains meaningful. Run IDs,
    service timestamps, and provider usage are deliberately outside the input.
    """
    data = _canonical(request)
    data["source_metadata"]["records"].sort(
        key=lambda item: (item["record_type"], item["record_id"])
    )
    snapshot = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    return snapshot, digest


class InterpretationProvider(Protocol):
    provider_type: ProviderType
    provider_name: str
    provider_version: str | None
    model_version: str | None
    prompt_version: str | None
    rule_version: str | None
    reviewer_id: str | None

    def interpret(
        self,
        project_context: ProjectInterpretationContext,
        hypothesis: Hypothesis,
        evidence_record: EvidenceRecord,
        source_metadata: SourceMetadataView,
    ) -> InterpretationAssessment: ...


@runtime_checkable
class ExternalSnapshotProvider(InterpretationProvider, Protocol):
    """Optional provider-neutral seam for an authorized external snapshot."""

    external_provider_name: str
    model_id: str
    data_destination: str

    def interpret_snapshot(self, snapshot: str) -> InterpretationAssessment: ...
    def get_provider_usage(self) -> ProviderUsage | None: ...
    def build_execution_metadata(
        self, run_id: str, authorization_id: str
    ) -> ProviderExecutionMetadata: ...


class MockInterpretationProvider:
    """Fixed synthetic assessment for infrastructure tests; no science rules."""

    provider_type = ProviderType.MOCK
    provider_name = "synthetic-mock"
    provider_version = "1"
    model_version = None
    prompt_version = None
    rule_version = None
    reviewer_id = None

    def __init__(self, assessment: InterpretationAssessment | dict[str, Any]):
        self.assessment = assessment

    def interpret(
        self,
        project_context: ProjectInterpretationContext,
        hypothesis: Hypothesis,
        evidence_record: EvidenceRecord,
        source_metadata: SourceMetadataView,
    ) -> InterpretationAssessment:
        return self.assessment


class InterpretationExecutionError(RuntimeError):
    def __init__(self, run_id: str, reason: str):
        self.run_id = run_id
        super().__init__(f"interpretation run {run_id} failed: {reason}")


class InterpretationService:
    """Own run lifecycle; providers only return scientific assessments."""

    def __init__(
        self,
        database: EvidenceDatabase,
        *,
        allow_external_provider: bool = False,
        usage_collector: Callable[[], ProviderUsage | None] | None = None,
    ) -> None:
        self.database = database
        self.allow_external_provider = allow_external_provider
        self.usage_collector = usage_collector

    def interpret(
        self, request: InterpretationRequest, provider: InterpretationProvider,
        *, authorization_id: str | None = None, run_id: str | None = None,
    ) -> tuple[InterpretationRun, EvidenceInterpretation]:
        self._validate_input(request, provider)
        snapshot, snapshot_hash = canonical_input_snapshot(request)
        external = isinstance(provider, ExternalSnapshotProvider)
        authorization: ExternalTransmissionAuthorization | None = None
        if external:
            authorization = self.database.get_external_authorization(authorization_id or "")
            if authorization is None or (
                authorization.project_id != request.project_context.project_name
                or authorization.snapshot_hash != snapshot_hash
                or authorization.input_snapshot != snapshot
                or authorization.provider != provider.external_provider_name
                or authorization.model != provider.model_id
                or authorization.data_destination != provider.data_destination
                or authorization.scope != "SINGLE_SNAPSHOT"
            ):
                raise ValueError("external transmission authorization required or mismatched")
        # Rehydrate from the persisted bytes: these are precisely the values
        # handed to the provider, even if the caller's objects later change.
        frozen = InterpretationRequest.model_validate_json(snapshot)
        started_at = datetime.now(timezone.utc)
        run = InterpretationRun(
            run_id=run_id if run_id is not None else f"IRUN_{uuid4().hex}",
            project_name=frozen.project_context.project_name,
            hypothesis_id=frozen.hypothesis.hypothesis_id,
            evidence_id=frozen.evidence_record.evidence_id,
            provider_type=ProviderType(provider.provider_type),
            provider_name=provider.provider_name,
            provider_version=provider.provider_version,
            model_version=provider.model_version,
            prompt_version=provider.prompt_version,
            rule_version=provider.rule_version,
            reviewer_id=provider.reviewer_id,
            input_snapshot=snapshot,
            input_snapshot_hash=snapshot_hash,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            snapshot_serialization_version=SNAPSHOT_SERIALIZATION_VERSION,
            snapshot_hash_algorithm=SNAPSHOT_HASH_ALGORITHM,
            started_at=started_at,
            status=InterpretationStatus.STARTED,
        )
        self.database.create_interpretation_run(run)
        start_ns = time.monotonic_ns()
        usage: ProviderUsage | None = None
        execution_metadata: ProviderExecutionMetadata | None = None
        try:
            if external:
                candidate = provider.interpret_snapshot(snapshot)
            else:
                candidate = provider.interpret(
                    frozen.project_context, frozen.hypothesis,
                    frozen.evidence_record, frozen.source_metadata,
                )
            assessment = InterpretationAssessment.model_validate(
                candidate.model_dump(mode="python") if isinstance(candidate, BaseModel) else candidate
            )
            validate_rationale_references(frozen, assessment.rationale_references)
            if run.provider_type is ProviderType.LLM and assessment.confidence is not None:
                raise ValueError("LLM confidence requires a calibration protocol")
            usage = (
                provider.get_provider_usage() if external else
                self.usage_collector() if self.usage_collector else None
            )
            if usage is not None:
                usage = ProviderUsage.model_validate(usage)
            if external:
                execution_metadata = ProviderExecutionMetadata.model_validate(
                    provider.build_execution_metadata(
                        run.run_id, authorization.authorization_id
                    )
                )
            completed_at = datetime.now(timezone.utc)
            done = run.model_copy(update={
                "status": InterpretationStatus.SUCCEEDED,
                "completed_at": completed_at,
                "latency_ms": (time.monotonic_ns() - start_ns) // 1_000_000,
                "provider_usage": usage,
            })
            interpretation = EvidenceInterpretation(
                interpretation_id=f"I_{uuid4().hex}", run_id=run.run_id,
                project_name=run.project_name, hypothesis_id=run.hypothesis_id,
                evidence_id=run.evidence_id, support=assessment.support,
                directness=assessment.directness, design_type=assessment.design_type,
                mechanistic_relevance=assessment.mechanistic_relevance,
                rationale=assessment.rationale,
                rationale_references=assessment.rationale_references,
                created_at=completed_at, confidence=assessment.confidence,
            )
            self.database.finish_interpretation_run(
                done, interpretation, execution_metadata
            )
            return done, interpretation
        except Exception as exc:
            # Source text and snapshots are never copied into errors or logs.
            category = getattr(exc, "category", None)
            reason = (
                category if isinstance(category, str) and category in SAFE_PROVIDER_FAILURES
                else None
            ) or (
                "provider output invalid" if isinstance(exc, (ValueError, TypeError))
                else "provider execution failed"
            )
            if usage is None:
                try:
                    raw_usage = (
                        provider.get_provider_usage() if external else
                        self.usage_collector() if self.usage_collector else None
                    )
                    usage = ProviderUsage.model_validate(raw_usage) if raw_usage is not None else None
                except Exception:
                    usage = None
            if external and execution_metadata is None:
                try:
                    execution_metadata = provider.build_execution_metadata(
                        run.run_id, authorization.authorization_id
                    )
                except Exception:
                    execution_metadata = None
            failed = run.model_copy(update={
                "status": InterpretationStatus.FAILED,
                "completed_at": datetime.now(timezone.utc),
                "latency_ms": (time.monotonic_ns() - start_ns) // 1_000_000,
                "provider_usage": usage,
                "error": reason,
            })
            self.database.finish_interpretation_run(failed, None, execution_metadata)
            raise InterpretationExecutionError(run.run_id, reason) from None

    def prepare_external_transmission(
        self, request: InterpretationRequest, provider: ExternalSnapshotProvider
    ) -> ExternalTransmissionPreparation:
        """Freeze the same bytes execution will persist, without network access."""
        if not isinstance(provider, ExternalSnapshotProvider):
            raise TypeError("provider does not support external snapshot interpretation")
        self._validate_input(request, provider, require_external_enabled=False)
        snapshot, digest = canonical_input_snapshot(request)
        return ExternalTransmissionPreparation(
            project_id=request.project_context.project_name,
            input_snapshot=snapshot, snapshot_hash=digest,
            provider=provider.external_provider_name, model=provider.model_id,
            data_destination=provider.data_destination,
        )

    def authorize_external_transmission(
        self, preparation: ExternalTransmissionPreparation, *, reviewed_snapshot: str
    ) -> ExternalTransmissionAuthorization:
        """Persist one explicit user-review action; never called by interpret."""
        if reviewed_snapshot != preparation.input_snapshot:
            raise ValueError("reviewed snapshot differs from prepared snapshot")
        if self.database.get_project(preparation.project_id) is None:
            raise ValueError("authorization project is not stored")
        authorization = ExternalTransmissionAuthorization(
            authorization_id=f"EAUTH_{uuid4().hex}",
            project_id=preparation.project_id,
            input_snapshot=preparation.input_snapshot,
            snapshot_hash=preparation.snapshot_hash,
            provider=preparation.provider, model=preparation.model,
            data_destination=preparation.data_destination,
            authorized_at=datetime.now(timezone.utc),
        )
        return self.database.save_external_authorization(authorization)

    def _validate_input(
        self, request: InterpretationRequest, provider: InterpretationProvider,
        *, require_external_enabled: bool = True,
    ) -> None:
        if isinstance(request.hypothesis, HypothesisDraft):
            raise ValueError("unconfirmed hypothesis draft cannot be interpreted")
        project = self.database.get_project(request.project_context.project_name)
        if project is None:
            raise ValueError("project is not persisted")
        context = request.project_context
        if (
            context.disease != project.disease
            or context.phenotype != project.phenotype
            or context.treatment != project.drug_or_treatment
            or context.organism != project.organism
            or context.tissue != project.tissue
            or context.research_question != project.research_question
            or context.gene not in project.candidate_genes
        ):
            raise ValueError("project context does not match persisted project")
        hypothesis = self.database.get_hypothesis(request.hypothesis.hypothesis_id)
        if hypothesis is None or hypothesis != request.hypothesis or hypothesis.project_name != project.project_name:
            raise ValueError("hypothesis is not confirmed in this project")
        evidence = self.database.get_evidence(request.evidence_record.evidence_id)
        if evidence is None or evidence != request.evidence_record or evidence.gene != context.gene:
            raise ValueError("evidence is not the persisted item for this gene")
        row = self.database._connection.execute(
            "SELECT project_name FROM evidence_record WHERE evidence_id = ?",
            (evidence.evidence_id,),
        ).fetchone()
        if row["project_name"] not in (None, project.project_name):
            raise ValueError("evidence belongs to another project")
        if evidence.support is not Support.UNKNOWN or evidence.directness is not Directness.UNASSESSED:
            raise ValueError("raw evidence must remain UNKNOWN/UNASSESSED")
        if any(
            record.evidence_id != evidence.evidence_id or record.record_type == "EvidenceRecord"
            for record in request.source_metadata.records
        ):
            raise ValueError("source metadata must belong to the one evidence item")
        if isinstance(provider, ExternalSnapshotProvider):
            self._validate_external_source(request)
        if (require_external_enabled and
            ProviderType(provider.provider_type) in (ProviderType.LLM, ProviderType.JEV)
            and not self.allow_external_provider):
            raise ValueError("external provider use requires explicit configuration")
        if ProviderType(provider.provider_type) is ProviderType.HUMAN and not provider.reviewer_id:
            raise ValueError("human provider requires a pseudonymous reviewer ID")

    def _validate_external_source(self, request: InterpretationRequest) -> None:
        evidence = request.evidence_record
        actual = self.database.get_source_metadata_view(evidence.evidence_id)
        def keyed(view: SourceMetadataView) -> dict[tuple[str, str], dict[str, Any]]:
            return {(row.record_type, row.record_id): row.model_dump(mode="json")
                    for row in view.records}
        if keyed(request.source_metadata) != keyed(actual):
            raise ValueError("source metadata differs from persisted evidence")
        if evidence.source_scope is SourceScope.USER:
            if actual.records:
                raise ValueError("USER source metadata is unsupported in 4B1")
        elif (
            evidence.source_scope is SourceScope.PUBLIC
            and evidence.source_name == "PubMed"
            and evidence.source_type == "LITERATURE"
        ):
            kinds = [row.record_type for row in actual.records]
            if kinds.count("PublicationMetadata") != 1 or "QueryMatch" not in kinds or any(
                kind not in ("PublicationMetadata", "QueryMatch") for kind in kinds
            ):
                raise ValueError("PubMed publication and query provenance are required")
        else:
            raise ValueError("evidence source is unsupported for external interpretation")
