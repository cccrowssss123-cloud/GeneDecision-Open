"""SQLite interpretation tables and persistence operations."""

from __future__ import annotations

import sqlite3
import hashlib
import json
from datetime import datetime

from genedecision.errors import EvidenceStorageError
from genedecision.models.manual_bridge import ManualBridgeExecutionMetadata
from genedecision.models.interpretation import (
    EvidenceInterpretation,
    ExternalTransmissionAuthorization,
    Hypothesis,
    InterpretationRequest,
    InterpretationRun,
    InterpretationStatus,
    ProviderUsage,
    ProviderExecutionMetadata,
    SourceFieldReference,
    SourceMetadataView,
    SourceRecord,
    validate_rationale_references,
)


MANUAL_BRIDGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS manual_bridge_execution_metadata (
    run_id TEXT NOT NULL PRIMARY KEY REFERENCES interpretation_run(run_id) ON DELETE CASCADE,
    metadata_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS manual_bridge_metadata_no_update
BEFORE UPDATE ON manual_bridge_execution_metadata
BEGIN SELECT RAISE(ABORT, 'manual bridge metadata is immutable'); END;
"""

INTERPRETATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hypothesis (
    hypothesis_id TEXT NOT NULL PRIMARY KEY,
    project_name TEXT NOT NULL REFERENCES project (project_name) ON DELETE CASCADE,
    exact_text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('USER_AUTHORED','GENERATED_AND_CONFIRMED')),
    confirmed_at TEXT NOT NULL,
    UNIQUE (project_name, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_project ON hypothesis (project_name);
CREATE TRIGGER IF NOT EXISTS hypothesis_no_update BEFORE UPDATE ON hypothesis
BEGIN SELECT RAISE(ABORT, 'confirmed hypotheses are immutable'); END;

CREATE TABLE IF NOT EXISTS interpretation_run (
    run_id TEXT NOT NULL PRIMARY KEY,
    project_name TEXT NOT NULL REFERENCES project (project_name) ON DELETE CASCADE,
    hypothesis_id TEXT NOT NULL REFERENCES hypothesis (hypothesis_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence_record (evidence_id) ON DELETE CASCADE,
    provider_type TEXT NOT NULL CHECK (provider_type IN ('HUMAN','RULE_BASED','LLM','JEV','MOCK')),
    provider_name TEXT NOT NULL,
    provider_version TEXT,
    model_version TEXT,
    prompt_version TEXT,
    rule_version TEXT,
    reviewer_id TEXT,
    input_snapshot TEXT NOT NULL,
    input_snapshot_hash TEXT NOT NULL,
    snapshot_schema_version TEXT NOT NULL,
    snapshot_serialization_version TEXT NOT NULL,
    snapshot_hash_algorithm TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('STARTED','SUCCEEDED','FAILED')),
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_interpretation_run_project
    ON interpretation_run (project_name, started_at);
CREATE INDEX IF NOT EXISTS idx_interpretation_run_item
    ON interpretation_run (evidence_id, hypothesis_id, provider_type);
CREATE INDEX IF NOT EXISTS idx_interpretation_run_snapshot
    ON interpretation_run (input_snapshot_hash);
CREATE TRIGGER IF NOT EXISTS interpretation_run_no_terminal_update
BEFORE UPDATE ON interpretation_run
WHEN OLD.status IN ('SUCCEEDED','FAILED')
BEGIN SELECT RAISE(ABORT, 'terminal interpretation runs are immutable'); END;

CREATE TABLE IF NOT EXISTS provider_usage (
    run_id TEXT NOT NULL PRIMARY KEY REFERENCES interpretation_run (run_id) ON DELETE CASCADE,
    input_units REAL CHECK (input_units IS NULL OR input_units >= 0),
    output_units REAL CHECK (output_units IS NULL OR output_units >= 0),
    billable_units REAL CHECK (billable_units IS NULL OR billable_units >= 0),
    unit_type TEXT,
    currency TEXT,
    estimated_cost_micros INTEGER CHECK (
        estimated_cost_micros IS NULL OR estimated_cost_micros >= 0
    ),
    pricing_version TEXT,
    reasoning_units REAL CHECK (reasoning_units IS NULL OR reasoning_units >= 0)
);

CREATE TABLE IF NOT EXISTS evidence_interpretation (
    interpretation_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES interpretation_run (run_id) ON DELETE CASCADE,
    project_name TEXT NOT NULL REFERENCES project (project_name) ON DELETE CASCADE,
    hypothesis_id TEXT NOT NULL REFERENCES hypothesis (hypothesis_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence_record (evidence_id) ON DELETE CASCADE,
    support TEXT NOT NULL CHECK (support IN ('SUPPORT','CONTRADICT','NEUTRAL','UNKNOWN')),
    directness TEXT NOT NULL CHECK (directness IN ('DIRECT','INDIRECT','CONTEXT_ONLY','UNKNOWN')),
    design_type TEXT NOT NULL CHECK (design_type IN ('PERTURBATIONAL','OBSERVATIONAL','ASSOCIATIONAL','UNKNOWN')),
    mechanistic_relevance TEXT NOT NULL CHECK (mechanistic_relevance IN ('MECHANISTIC','NON_MECHANISTIC','UNKNOWN')),
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
CREATE INDEX IF NOT EXISTS idx_interpretation_item
    ON evidence_interpretation (project_name, evidence_id, hypothesis_id);
CREATE TRIGGER IF NOT EXISTS interpretation_requires_successful_run
BEFORE INSERT ON evidence_interpretation
WHEN NOT EXISTS (
    SELECT 1 FROM interpretation_run AS run
    WHERE run.run_id = NEW.run_id AND run.status = 'SUCCEEDED'
      AND run.project_name = NEW.project_name
      AND run.hypothesis_id = NEW.hypothesis_id
      AND run.evidence_id = NEW.evidence_id
)
BEGIN SELECT RAISE(ABORT, 'interpretation requires matching successful run'); END;
CREATE TRIGGER IF NOT EXISTS evidence_interpretation_no_update
BEFORE UPDATE ON evidence_interpretation
BEGIN SELECT RAISE(ABORT, 'scientific interpretations are immutable'); END;

CREATE TABLE IF NOT EXISTS interpretation_reference (
    interpretation_id TEXT NOT NULL REFERENCES evidence_interpretation (interpretation_id)
        ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    quote TEXT,
    text_start INTEGER,
    text_end INTEGER,
    PRIMARY KEY (interpretation_id, ordinal)
);
"""


EXTERNAL_PROVIDER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS external_transmission_authorization (
    authorization_id TEXT NOT NULL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project (project_name) ON DELETE CASCADE,
    input_snapshot TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    data_destination TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope = 'SINGLE_SNAPSHOT')
);
CREATE INDEX IF NOT EXISTS idx_external_authorization_scope
    ON external_transmission_authorization
    (project_id, snapshot_hash, provider, model, data_destination);
CREATE TRIGGER IF NOT EXISTS external_authorization_no_update
BEFORE UPDATE ON external_transmission_authorization
BEGIN SELECT RAISE(ABORT, 'external authorizations are immutable'); END;

CREATE TABLE IF NOT EXISTS provider_execution_metadata (
    run_id TEXT NOT NULL PRIMARY KEY REFERENCES interpretation_run (run_id) ON DELETE CASCADE,
    adapter_name TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    model_id TEXT NOT NULL,
    provider_response_id TEXT,
    prompt_template_version TEXT NOT NULL,
    prompt_template_hash TEXT NOT NULL,
    output_schema_version TEXT NOT NULL,
    output_schema_hash TEXT NOT NULL,
    provider_execution_configuration TEXT NOT NULL,
    data_destination TEXT NOT NULL,
    authorization_id TEXT NOT NULL REFERENCES external_transmission_authorization (authorization_id),
    attempt_count INTEGER NOT NULL CHECK (attempt_count IN (0, 1)),
    provider_reported_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_execution_authorization
    ON provider_execution_metadata (authorization_id);
CREATE TRIGGER IF NOT EXISTS provider_execution_no_update
BEFORE UPDATE ON provider_execution_metadata
BEGIN SELECT RAISE(ABORT, 'provider execution metadata is immutable'); END;
"""


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _usage_from_db(connection: sqlite3.Connection, run_id: str) -> ProviderUsage | None:
    row = connection.execute(
        "SELECT * FROM provider_usage WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    return ProviderUsage(**{key: row[key] for key in ProviderUsage.model_fields})


def _run_from_db(connection: sqlite3.Connection, row: sqlite3.Row) -> InterpretationRun:
    data = {key: row[key] for key in InterpretationRun.model_fields if key != "provider_usage"}
    data["started_at"] = _datetime(data["started_at"])
    data["completed_at"] = _datetime(data["completed_at"])
    data["provider_usage"] = _usage_from_db(connection, row["run_id"])
    return InterpretationRun(**data)


class InterpretationStorageMixin:
    """Methods mixed into EvidenceDatabase; its connection has foreign keys ON."""

    _connection: sqlite3.Connection

    def save_manual_bridge_metadata(self, metadata: ManualBridgeExecutionMetadata) -> None:
        run = self.get_interpretation_run(metadata.run_id)
        if (run is None or run.provider_name != "Codex" or run.provider_type.value != "LLM"
            or run.input_snapshot_hash != metadata.snapshot_hash
            or run.provider_usage is not None
            or self.get_provider_execution_metadata(run.run_id) is not None
        ):
            raise EvidenceStorageError("manual provenance does not match local Codex run")
        existing = self.get_manual_bridge_metadata(metadata.run_id)
        if existing is not None:
            if existing != metadata:
                raise EvidenceStorageError("manual provenance is immutable")
            return
        with self._connection:
            self._connection.execute(
                "INSERT INTO manual_bridge_execution_metadata VALUES (?,?)",
                (metadata.run_id, metadata.model_dump_json()),
            )

    def get_manual_bridge_metadata(self, run_id: str) -> ManualBridgeExecutionMetadata | None:
        row = self._connection.execute(
            "SELECT metadata_json FROM manual_bridge_execution_metadata WHERE run_id=?", (run_id,)
        ).fetchone()
        return None if row is None else ManualBridgeExecutionMetadata.model_validate_json(row[0])

    def save_external_authorization(
        self, authorization: ExternalTransmissionAuthorization
    ) -> ExternalTransmissionAuthorization:
        if self.get_project(authorization.project_id) is None:
            raise EvidenceStorageError("authorization project is not stored")
        request = InterpretationRequest.model_validate_json(authorization.input_snapshot)
        if request.project_context.project_name != authorization.project_id:
            raise EvidenceStorageError("authorization snapshot is for another project")
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO external_transmission_authorization VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        authorization.authorization_id, authorization.project_id,
                        authorization.input_snapshot, authorization.snapshot_hash,
                        authorization.provider, authorization.model,
                        authorization.data_destination, authorization.authorized_at.isoformat(),
                        authorization.scope,
                    ),
                )
        except sqlite3.IntegrityError:
            raise EvidenceStorageError("cannot save external authorization") from None
        return authorization

    def get_external_authorization(
        self, authorization_id: str
    ) -> ExternalTransmissionAuthorization | None:
        row = self._connection.execute(
            "SELECT * FROM external_transmission_authorization WHERE authorization_id = ?",
            (authorization_id,),
        ).fetchone()
        return None if row is None else ExternalTransmissionAuthorization(
            **{key: row[key] for key in ExternalTransmissionAuthorization.model_fields
               if key != "authorized_at"},
            authorized_at=_datetime(row["authorized_at"]),
        )

    def get_provider_execution_metadata(
        self, run_id: str
    ) -> ProviderExecutionMetadata | None:
        row = self._connection.execute(
            "SELECT * FROM provider_execution_metadata WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        data = {key: row[key] for key in ProviderExecutionMetadata.model_fields}
        data["provider_execution_configuration"] = json.loads(
            data["provider_execution_configuration"]
        )
        return ProviderExecutionMetadata(**data)

    def get_source_metadata_view(self, evidence_id: str) -> SourceMetadataView:
        """Build a source-scoped factual view for one stored evidence item."""
        if self.get_evidence(evidence_id) is None:
            raise EvidenceStorageError("evidence item is not stored")
        records: list[SourceRecord] = []

        def add(record_type: str, record_id: str, model: object) -> None:
            records.append(SourceRecord(
                record_type=record_type, record_id=record_id,
                evidence_id=evidence_id,
                fields=model.model_dump(mode="json"),
            ))

        publication = self.get_publication_metadata(evidence_id)
        if publication is not None:
            add("PublicationMetadata", evidence_id, publication)
        for match in self.list_query_matches(evidence_id=evidence_id):
            identity = hashlib.sha256(
                f"{match.run_id}\0{match.evidence_id}\0{match.query_text}".encode("utf-8")
            ).hexdigest()
            add("QueryMatch", f"QM_{identity}", match)

        identity = self.get_depmap_gene_identity(evidence_id)
        if identity is not None:
            add("DepMapGeneIdentity", evidence_id, identity)
            observations = self.list_depmap_observations(evidence_id=evidence_id)
            annotations = self.list_common_essential_annotations(evidence_id=evidence_id)
            for observation in observations:
                add("DepMapDependencyObservation", observation.observation_id, observation)
            for annotation in annotations:
                add("CommonEssentialAnnotation", f"{evidence_id}:{annotation.annotation_type.value}", annotation)
            release_ids = {item.release_id for item in observations + annotations}
            for release_id in sorted(release_ids):
                release = self.get_depmap_release(release_id)
                if release is not None:
                    add("DepMapReleaseMetadata", release_id, release)
                for source_file in self.list_depmap_dataset_files(release_id):
                    add("DepMapDatasetFile", source_file.dataset_file_id, source_file)
                for model in self.list_depmap_model_metadata(release_id=release_id):
                    add("DepMapModelMetadata", f"{release_id}:{model.model_id}", model)
        return SourceMetadataView(records=tuple(records))

    def save_hypothesis(self, hypothesis: Hypothesis) -> Hypothesis:
        """Insert confirmed immutable content; an existing identity is never updated."""
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT OR IGNORE INTO hypothesis"
                    " (hypothesis_id, project_name, exact_text, text_hash, origin, confirmed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        hypothesis.hypothesis_id,
                        hypothesis.project_name,
                        hypothesis.exact_text,
                        hypothesis.text_hash,
                        hypothesis.origin.value,
                        hypothesis.confirmed_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise EvidenceStorageError("cannot save hypothesis for an absent project") from exc
        stored = self.get_hypothesis(hypothesis.hypothesis_id)
        if stored is None or (
            stored.project_name != hypothesis.project_name
            or stored.exact_text != hypothesis.exact_text
            or stored.text_hash != hypothesis.text_hash
        ):
            raise EvidenceStorageError("hypothesis identity conflicts with stored content")
        return stored

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        row = self._connection.execute(
            "SELECT * FROM hypothesis WHERE hypothesis_id = ?", (hypothesis_id,)
        ).fetchone()
        return None if row is None else Hypothesis(
            hypothesis_id=row["hypothesis_id"],
            project_name=row["project_name"],
            exact_text=row["exact_text"],
            text_hash=row["text_hash"],
            origin=row["origin"],
            confirmed_at=_datetime(row["confirmed_at"]),
        )

    def list_hypotheses(self, project_name: str) -> list[Hypothesis]:
        rows = self._connection.execute(
            "SELECT hypothesis_id FROM hypothesis WHERE project_name = ?"
            " ORDER BY confirmed_at, hypothesis_id", (project_name,)
        ).fetchall()
        return [self.get_hypothesis(row["hypothesis_id"]) for row in rows]

    def create_interpretation_run(self, run: InterpretationRun) -> None:
        if run.status is not InterpretationStatus.STARTED or run.completed_at is not None:
            raise ValueError("new interpretation run must be STARTED")
        if run.provider_usage is not None or run.error is not None:
            raise ValueError("new interpretation run cannot have usage or error")
        hypothesis = self.get_hypothesis(run.hypothesis_id)
        if hypothesis is None or hypothesis.project_name != run.project_name:
            raise EvidenceStorageError("interpretation hypothesis is not in project")
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO interpretation_run ("
                    "run_id, project_name, hypothesis_id, evidence_id, provider_type,"
                    "provider_name, provider_version, model_version, prompt_version,"
                    "rule_version, reviewer_id, input_snapshot, input_snapshot_hash,"
                    "snapshot_schema_version, snapshot_serialization_version,"
                    "snapshot_hash_algorithm, started_at, completed_at, latency_ms,"
                    "status, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run.run_id, run.project_name, run.hypothesis_id, run.evidence_id,
                        run.provider_type.value, run.provider_name, run.provider_version,
                        run.model_version, run.prompt_version, run.rule_version,
                        run.reviewer_id, run.input_snapshot, run.input_snapshot_hash,
                        run.snapshot_schema_version, run.snapshot_serialization_version,
                        run.snapshot_hash_algorithm, run.started_at.isoformat(), None,
                        None, run.status.value, None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise EvidenceStorageError(f"cannot create interpretation run: {exc}") from exc

    def finish_interpretation_run(
        self, run: InterpretationRun, interpretation: EvidenceInterpretation | None,
        execution_metadata: ProviderExecutionMetadata | None = None,
    ) -> None:
        if run.status not in (InterpretationStatus.SUCCEEDED, InterpretationStatus.FAILED):
            raise ValueError("completed run must be SUCCEEDED or FAILED")
        if run.completed_at is None:
            raise ValueError("completed run needs completed_at")
        if (run.status is InterpretationStatus.SUCCEEDED) != (interpretation is not None):
            raise ValueError("only a successful run can have an interpretation")
        if interpretation is not None and (
            interpretation.run_id != run.run_id
            or interpretation.project_name != run.project_name
            or interpretation.hypothesis_id != run.hypothesis_id
            or interpretation.evidence_id != run.evidence_id
        ):
            raise ValueError("interpretation identity does not match run")
        if execution_metadata is not None and execution_metadata.run_id != run.run_id:
            raise ValueError("execution metadata identity does not match run")
        if execution_metadata is not None:
            authorization = self.get_external_authorization(execution_metadata.authorization_id)
            if authorization is None or (
                authorization.project_id != run.project_name
                or authorization.input_snapshot != run.input_snapshot
                or authorization.snapshot_hash != run.input_snapshot_hash
                or authorization.provider != run.provider_name
                or authorization.model != execution_metadata.model_id
                or authorization.data_destination != execution_metadata.data_destination
            ):
                raise EvidenceStorageError("execution metadata authorization does not match run")
        stored = self.get_interpretation_run(run.run_id)
        if stored is None or stored.status is not InterpretationStatus.STARTED:
            raise EvidenceStorageError("run is absent or already completed")
        frozen_fields = (
            "project_name", "hypothesis_id", "evidence_id", "provider_type",
            "provider_name", "provider_version", "model_version", "prompt_version",
            "rule_version", "reviewer_id", "input_snapshot", "input_snapshot_hash",
            "snapshot_schema_version", "snapshot_serialization_version",
            "snapshot_hash_algorithm", "started_at",
        )
        if any(getattr(run, field) != getattr(stored, field) for field in frozen_fields):
            raise EvidenceStorageError("run identity or provider input was changed")
        if interpretation is not None:
            request = InterpretationRequest.model_validate_json(stored.input_snapshot)
            validate_rationale_references(request, interpretation.rationale_references)
        try:
            with self._connection:
                result = self._connection.execute(
                    "UPDATE interpretation_run SET completed_at = ?, latency_ms = ?,"
                    " status = ?, error = ? WHERE run_id = ? AND status = 'STARTED'",
                    (
                        run.completed_at.isoformat(), run.latency_ms,
                        run.status.value, run.error, run.run_id,
                    ),
                )
                if result.rowcount != 1:
                    raise EvidenceStorageError("run is absent or already completed")
                if run.provider_usage is not None:
                    usage = run.provider_usage
                    self._connection.execute(
                        "INSERT INTO provider_usage (run_id, input_units, output_units,"
                        " billable_units, unit_type, currency, estimated_cost_micros,"
                        " pricing_version, reasoning_units) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            run.run_id, usage.input_units, usage.output_units,
                            usage.billable_units, usage.unit_type, usage.currency,
                            usage.estimated_cost_micros, usage.pricing_version,
                            usage.reasoning_units,
                        ),
                    )
                if execution_metadata is not None:
                    meta = execution_metadata
                    self._connection.execute(
                        "INSERT INTO provider_execution_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            meta.run_id, meta.adapter_name, meta.adapter_version,
                            meta.model_id, meta.provider_response_id,
                            meta.prompt_template_version, meta.prompt_template_hash,
                            meta.output_schema_version, meta.output_schema_hash,
                            json.dumps(meta.provider_execution_configuration, sort_keys=True,
                                       separators=(",", ":"), allow_nan=False),
                            meta.data_destination, meta.authorization_id,
                            meta.attempt_count, meta.provider_reported_model,
                        ),
                    )
                if interpretation is not None:
                    self._connection.execute(
                        "INSERT INTO evidence_interpretation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            interpretation.interpretation_id, interpretation.run_id,
                            interpretation.project_name, interpretation.hypothesis_id,
                            interpretation.evidence_id, interpretation.support.value,
                            interpretation.directness.value, interpretation.design_type.value,
                            interpretation.mechanistic_relevance.value,
                            interpretation.rationale, interpretation.created_at.isoformat(),
                            interpretation.confidence,
                        ),
                    )
                    for ordinal, ref in enumerate(interpretation.rationale_references):
                        self._connection.execute(
                            "INSERT INTO interpretation_reference VALUES (?,?,?,?,?,?,?,?)",
                            (
                                interpretation.interpretation_id, ordinal,
                                ref.record_type, ref.record_id, ref.field_name,
                                ref.quote, ref.text_start, ref.text_end,
                            ),
                        )
        except sqlite3.IntegrityError as exc:
            raise EvidenceStorageError(f"cannot finish interpretation run: {exc}") from exc

    def get_interpretation_run(self, run_id: str) -> InterpretationRun | None:
        row = self._connection.execute(
            "SELECT * FROM interpretation_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _run_from_db(self._connection, row)

    def get_interpretation(self, interpretation_id: str) -> EvidenceInterpretation | None:
        row = self._connection.execute(
            "SELECT * FROM evidence_interpretation WHERE interpretation_id = ?",
            (interpretation_id,),
        ).fetchone()
        if row is None:
            return None
        refs = self._connection.execute(
            "SELECT * FROM interpretation_reference WHERE interpretation_id = ?"
            " ORDER BY ordinal", (interpretation_id,),
        ).fetchall()
        return EvidenceInterpretation(
            interpretation_id=row["interpretation_id"], run_id=row["run_id"],
            project_name=row["project_name"], hypothesis_id=row["hypothesis_id"],
            evidence_id=row["evidence_id"], support=row["support"],
            directness=row["directness"], design_type=row["design_type"],
            mechanistic_relevance=row["mechanistic_relevance"],
            rationale=row["rationale"], created_at=_datetime(row["created_at"]),
            confidence=row["confidence"],
            rationale_references=tuple(
                SourceFieldReference(
                    record_type=ref["record_type"], record_id=ref["record_id"],
                    field_name=ref["field_name"], quote=ref["quote"],
                    text_start=ref["text_start"], text_end=ref["text_end"],
                ) for ref in refs
            ),
        )

    def list_interpretations(
        self, *, evidence_id: str | None = None, hypothesis_id: str | None = None
    ) -> list[EvidenceInterpretation]:
        conditions: list[str] = []
        params: list[str] = []
        if evidence_id is not None:
            conditions.append("evidence_id = ?")
            params.append(evidence_id)
        if hypothesis_id is not None:
            conditions.append("hypothesis_id = ?")
            params.append(hypothesis_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self._connection.execute(
            "SELECT interpretation_id FROM evidence_interpretation" + where
            + " ORDER BY created_at, interpretation_id", params,
        ).fetchall()
        return [self.get_interpretation(row["interpretation_id"]) for row in rows]

    def delete_project(self, project_name: str) -> bool:
        """Delete project interpretation data and its private snapshots atomically.

        Interpretation tables also have ON DELETE CASCADE for direct SQL
        project deletion. Existing evidence_record uses a restrictive project
        FK. Detach linked factual evidence before deleting the project; raw
        User, PubMed and DepMap records and their source metadata survive.
        """
        with self._connection:
            self._connection.execute(
                "UPDATE evidence_record SET project_name = NULL WHERE project_name = ?",
                (project_name,),
            )
            result = self._connection.execute(
                "DELETE FROM project WHERE project_name = ?", (project_name,)
            )
        return result.rowcount == 1
