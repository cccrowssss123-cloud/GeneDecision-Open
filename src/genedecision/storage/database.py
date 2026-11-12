"""SQLite persistence for user projects, their evidence, and collected sources.

Design notes
------------
* Every ``EvidenceRecord`` field is stored in **its own column** so individual
  scientific values stay queryable with plain SQL. Only the two list-valued
  fields (``Project.candidate_genes`` and ``EvidenceRecord.quality_flags``) are
  stored as canonical JSON text, because they are lists rather than scalars.
* ``None`` is stored as SQL ``NULL`` and read back as ``None``; supplied numbers
  are stored in SQLite ``REAL``/``INTEGER`` columns and round-trip bit-exactly.
* Writing the same ``evidence_id`` twice is deterministic: the default
  ``on_conflict="replace"`` makes a re-import idempotent, while
  ``on_conflict="error"`` refuses the overwrite.
* Publication metadata and exact query matches use separate relational tables;
  neither is hidden inside ``EvidenceRecord`` or an opaque object blob.
* DepMap factual collection data (release, dataset/file identity, licensing
  provenance, resolved gene identity, minimal model metadata, measurement-level
  observations and per-definition essentiality annotations) also gets its own
  tables in schema v3. Runtime-only DepMap aggregates — ``DepMapQueryResult``,
  ``DepMapRunResult``, ``DepMapDependencyMetadata`` and
  ``DepMapContextCoverage`` — deliberately have **no** table: every field in
  them is recoverable from these factual records.
* Saving a DepMap run writes rows whose identity is deterministic, so
  re-importing the same release/file/profile upserts instead of duplicating.
  A conflicting *source* checksum and a changed *local* integrity hash are both
  refused, and are reported as different failures.
* This module is independent of any future UI.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel

from genedecision.connectors.base import (
    ConnectorRunResult,
    PublicationMetadata,
    QueryMatch,
)
from genedecision.connectors.depmap import DepMapRunResult
from genedecision.errors import DuplicateEvidenceError, EvidenceStorageError
from genedecision.models.depmap import (
    CommonEssentialAnnotation,
    DataLicenseMetadata,
    DepMapDatasetFile,
    DepMapDependencyObservation,
    DepMapGeneIdentity,
    DepMapModelMetadata,
    DepMapReleaseMetadata,
)
from genedecision.models.evidence import EvidenceRecord
from genedecision.models.project import Project
from genedecision.storage.interpretation import (
    EXTERNAL_PROVIDER_SCHEMA_SQL,
    INTERPRETATION_SCHEMA_SQL,
    MANUAL_BRIDGE_SCHEMA_SQL,
    InterpretationStorageMixin,
)
from genedecision.storage.decision import DECISION_SCHEMA_SQL, DecisionStorageMixin

SCHEMA_VERSION = 8

OnConflict = Literal["replace", "error"]

#: Field order follows the schema definition itself, so a column can never be
#: silently missing behind a schema change.
EVIDENCE_FIELDS: tuple[str, ...] = tuple(EvidenceRecord.model_fields)
PROJECT_FIELDS: tuple[str, ...] = tuple(Project.model_fields)

#: Created before migrations run, so the stored schema version can be read on a
#: database that predates every other table.
_SCHEMA_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS project (
    project_name      TEXT NOT NULL PRIMARY KEY,
    organism          TEXT NOT NULL,
    disease           TEXT NOT NULL,
    tissue            TEXT NOT NULL,
    phenotype         TEXT NOT NULL,
    drug_or_treatment TEXT NOT NULL,
    research_question TEXT NOT NULL,
    candidate_genes   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_record (
    evidence_id          TEXT NOT NULL PRIMARY KEY,
    gene                 TEXT NOT NULL,
    source_scope         TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    source_name          TEXT,
    source_identifier    TEXT,
    source_url           TEXT,
    disease              TEXT,
    phenotype            TEXT,
    drug                 TEXT,
    experimental_context TEXT,
    comparison           TEXT,
    effect_type          TEXT,
    effect_value         REAL,
    p_value              REAL,
    fdr                  REAL,
    sample_size          INTEGER,
    cell_type            TEXT,
    species              TEXT,
    evidence_level       TEXT NOT NULL,
    support              TEXT NOT NULL,
    directness           TEXT NOT NULL,
    summary              TEXT,
    quality_flags        TEXT NOT NULL,
    retrieved_at         TEXT,
    project_name         TEXT REFERENCES project (project_name)
);

CREATE INDEX IF NOT EXISTS idx_evidence_record_gene
    ON evidence_record (gene);
CREATE INDEX IF NOT EXISTS idx_evidence_record_project
    ON evidence_record (project_name);

CREATE TABLE IF NOT EXISTS connector_run (
    run_id       TEXT NOT NULL PRIMARY KEY,
    connector    TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_metadata (
    evidence_id       TEXT NOT NULL PRIMARY KEY
                      REFERENCES evidence_record (evidence_id) ON DELETE CASCADE,
    pmid              TEXT NOT NULL,
    title             TEXT,
    abstract          TEXT,
    journal           TEXT,
    publication_date  TEXT,
    publication_year  INTEGER,
    authors           TEXT NOT NULL,
    publication_types TEXT NOT NULL,
    doi               TEXT,
    language          TEXT
);

CREATE TABLE IF NOT EXISTS query_match (
    run_id           TEXT NOT NULL
                     REFERENCES connector_run (run_id) ON DELETE CASCADE,
    evidence_id      TEXT NOT NULL
                     REFERENCES evidence_record (evidence_id) ON DELETE CASCADE,
    connector        TEXT NOT NULL,
    query_text       TEXT NOT NULL,
    query_type       TEXT NOT NULL,
    gene             TEXT NOT NULL,
    disease          TEXT,
    phenotype        TEXT,
    drug             TEXT,
    translated_query TEXT,
    retrieved_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, evidence_id, query_text)
);

CREATE INDEX IF NOT EXISTS idx_query_match_evidence
    ON query_match (evidence_id);
CREATE INDEX IF NOT EXISTS idx_query_match_run
    ON query_match (run_id);
"""

#: Schema v3: DepMap factual collection data.
#:
#: Only facts get tables here. ``DepMapQueryResult``, ``DepMapRunResult``,
#: ``DepMapDependencyMetadata`` and ``DepMapContextCoverage`` are runtime
#: aggregates and are intentionally absent.
#:
#: ``data_license_metadata`` has no ``commercial_use_allowed`` column and never
#: will: the connector records which terms reference accompanied a collection
#: run and the factual notes attached to it, and makes no legal determination.
#: DepMap connector support does not imply commercial redistribution rights.
_DEPMAP_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS depmap_release (
    release_id                              TEXT NOT NULL PRIMARY KEY,
    release_name                            TEXT NOT NULL,
    release_code                            TEXT,
    release_date                            TEXT,
    manifest_url                            TEXT,
    manifest_retrieved_at                   TEXT NOT NULL,
    manifest_local_integrity_hash_algorithm TEXT,
    manifest_local_integrity_hash_value     TEXT
);

CREATE TABLE IF NOT EXISTS depmap_dataset_file (
    dataset_file_id                     TEXT NOT NULL PRIMARY KEY,
    release_id                          TEXT NOT NULL
                                        REFERENCES depmap_release (release_id)
                                        ON DELETE CASCADE,
    dataset_name                        TEXT NOT NULL,
    filename                            TEXT NOT NULL,
    source_checksum_algorithm           TEXT,
    source_checksum_value               TEXT,
    local_file_integrity_hash_algorithm TEXT,
    local_file_integrity_hash_value     TEXT,
    official_file_identity              TEXT NOT NULL,
    source_url                          TEXT,
    retrieved_at                        TEXT NOT NULL,
    UNIQUE (release_id, dataset_name, filename)
);

CREATE INDEX IF NOT EXISTS idx_depmap_dataset_file_release
    ON depmap_dataset_file (release_id);

CREATE TABLE IF NOT EXISTS data_license_metadata (
    release_id         TEXT NOT NULL PRIMARY KEY
                       REFERENCES depmap_release (release_id) ON DELETE CASCADE,
    source             TEXT NOT NULL,
    release_name       TEXT NOT NULL,
    terms_url          TEXT,
    terms_retrieved_at TEXT,
    usage_note         TEXT,
    license_note       TEXT
);

CREATE TABLE IF NOT EXISTS depmap_run_release (
    run_id     TEXT NOT NULL
               REFERENCES connector_run (run_id) ON DELETE CASCADE,
    release_id TEXT NOT NULL
               REFERENCES depmap_release (release_id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, release_id)
);

CREATE TABLE IF NOT EXISTS depmap_run_dataset_file (
    run_id          TEXT NOT NULL
                    REFERENCES connector_run (run_id) ON DELETE CASCADE,
    dataset_file_id TEXT NOT NULL
                    REFERENCES depmap_dataset_file (dataset_file_id)
                    ON DELETE CASCADE,
    PRIMARY KEY (run_id, dataset_file_id)
);

CREATE TABLE IF NOT EXISTS depmap_gene_identity (
    evidence_id           TEXT NOT NULL PRIMARY KEY
                          REFERENCES evidence_record (evidence_id)
                          ON DELETE CASCADE,
    input_gene            TEXT NOT NULL,
    depmap_symbol         TEXT NOT NULL,
    entrez_gene_id        TEXT,
    raw_matrix_header     TEXT NOT NULL,
    resolution_basis      TEXT NOT NULL,
    gene_metadata_file_id TEXT NOT NULL
                          REFERENCES depmap_dataset_file (dataset_file_id)
);

CREATE TABLE IF NOT EXISTS depmap_model_metadata (
    release_id               TEXT NOT NULL
                             REFERENCES depmap_release (release_id)
                             ON DELETE CASCADE,
    model_id                 TEXT NOT NULL,
    cell_line_name           TEXT,
    depmap_model_type        TEXT,
    oncotree_lineage         TEXT,
    oncotree_primary_disease TEXT,
    oncotree_subtype         TEXT,
    source_file_id           TEXT NOT NULL
                             REFERENCES depmap_dataset_file (dataset_file_id),
    PRIMARY KEY (release_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_depmap_model_metadata_release
    ON depmap_model_metadata (release_id);

CREATE TABLE IF NOT EXISTS depmap_dependency_observation (
    observation_id       TEXT NOT NULL PRIMARY KEY,
    evidence_id          TEXT NOT NULL
                         REFERENCES evidence_record (evidence_id)
                         ON DELETE CASCADE,
    release_id           TEXT NOT NULL
                         REFERENCES depmap_release (release_id)
                         ON DELETE CASCADE,
    dataset_name         TEXT NOT NULL,
    source_file_id       TEXT NOT NULL
                         REFERENCES depmap_dataset_file (dataset_file_id),
    source_file_identity TEXT NOT NULL,
    model_id             TEXT NOT NULL,
    gene_symbol          TEXT NOT NULL,
    gene_identifier      TEXT,
    raw_gene_header      TEXT NOT NULL,
    measurement_type     TEXT NOT NULL,
    measurement_status   TEXT NOT NULL,
    value                REAL,
    raw_value            TEXT,
    retrieved_at         TEXT NOT NULL,
    quality_flags        TEXT NOT NULL,
    UNIQUE (release_id, source_file_id, model_id, gene_symbol, measurement_type)
);

CREATE INDEX IF NOT EXISTS idx_depmap_observation_evidence
    ON depmap_dependency_observation (evidence_id);
CREATE INDEX IF NOT EXISTS idx_depmap_observation_release_model
    ON depmap_dependency_observation (release_id, model_id);

CREATE TABLE IF NOT EXISTS depmap_common_essential_annotation (
    evidence_id       TEXT NOT NULL
                      REFERENCES evidence_record (evidence_id) ON DELETE CASCADE,
    release_id        TEXT NOT NULL
                      REFERENCES depmap_release (release_id) ON DELETE CASCADE,
    gene_symbol       TEXT NOT NULL,
    gene_identifier   TEXT,
    annotation_type   TEXT NOT NULL,
    source_dataset    TEXT NOT NULL,
    membership_status TEXT NOT NULL,
    source_definition TEXT NOT NULL,
    source_file_id    TEXT NOT NULL
                      REFERENCES depmap_dataset_file (dataset_file_id),
    provenance_note   TEXT,
    retrieved_at      TEXT NOT NULL,
    PRIMARY KEY (evidence_id, annotation_type)
);

CREATE INDEX IF NOT EXISTS idx_depmap_annotation_release
    ON depmap_common_essential_annotation (release_id);
"""

#: Everything the current schema version contains, created idempotently.
_SCHEMA_SQL = (
    _CORE_SCHEMA_SQL + _DEPMAP_SCHEMA_SQL + INTERPRETATION_SCHEMA_SQL
    + EXTERNAL_PROVIDER_SCHEMA_SQL
)

_MIGRATE_V1_TO_V2_SQL = """
CREATE TABLE evidence_record_v2 (
    evidence_id          TEXT NOT NULL PRIMARY KEY,
    gene                 TEXT NOT NULL,
    source_scope         TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    source_name          TEXT,
    source_identifier    TEXT,
    source_url           TEXT,
    disease              TEXT,
    phenotype            TEXT,
    drug                 TEXT,
    experimental_context TEXT,
    comparison           TEXT,
    effect_type          TEXT,
    effect_value         REAL,
    p_value              REAL,
    fdr                  REAL,
    sample_size          INTEGER,
    cell_type            TEXT,
    species              TEXT,
    evidence_level       TEXT NOT NULL,
    support              TEXT NOT NULL,
    directness           TEXT NOT NULL,
    summary              TEXT,
    quality_flags        TEXT NOT NULL,
    retrieved_at         TEXT,
    project_name         TEXT REFERENCES project (project_name)
);

INSERT INTO evidence_record_v2
SELECT * FROM evidence_record;
DROP TABLE evidence_record;
ALTER TABLE evidence_record_v2 RENAME TO evidence_record;

CREATE INDEX idx_evidence_record_gene ON evidence_record (gene);
CREATE INDEX idx_evidence_record_project ON evidence_record (project_name);
"""


def _insert_sql(table: str, fields: Sequence[str]) -> str:
    columns = ", ".join(fields)
    placeholders = ", ".join(f":{name}" for name in fields)
    return f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"


def _upsert_sql(table: str, fields: Sequence[str], key: str) -> str:
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in fields if name != key
    )
    return f"{_insert_sql(table, fields)} ON CONFLICT({key}) DO UPDATE SET {updates}"


_EVIDENCE_FIELDS_WITH_PROJECT = EVIDENCE_FIELDS + ("project_name",)
_EVIDENCE_INSERT = _insert_sql("evidence_record", _EVIDENCE_FIELDS_WITH_PROJECT)
_EVIDENCE_UPSERT = _upsert_sql(
    "evidence_record", _EVIDENCE_FIELDS_WITH_PROJECT, "evidence_id"
)
_PROJECT_INSERT = _insert_sql("project", PROJECT_FIELDS)
_PROJECT_UPSERT = _upsert_sql("project", PROJECT_FIELDS, "project_name")

PUBLICATION_FIELDS = (
    "evidence_id",
    "pmid",
    "title",
    "abstract",
    "journal",
    "publication_date",
    "publication_year",
    "authors",
    "publication_types",
    "doi",
    "language",
)
QUERY_MATCH_FIELDS = tuple(QueryMatch.model_fields)
CONNECTOR_RUN_FIELDS = ("run_id", "connector", "source_scope", "retrieved_at")

_PUBLICATION_UPSERT = _upsert_sql(
    "publication_metadata", PUBLICATION_FIELDS, "evidence_id"
)
_QUERY_MATCH_UPSERT = (
    _insert_sql("query_match", QUERY_MATCH_FIELDS)
    + " ON CONFLICT(run_id, evidence_id, query_text) DO UPDATE SET "
    + ", ".join(
        f"{name} = excluded.{name}"
        for name in QUERY_MATCH_FIELDS
        if name not in {"run_id", "evidence_id", "query_text"}
    )
)
_CONNECTOR_RUN_UPSERT = _upsert_sql(
    "connector_run", CONNECTOR_RUN_FIELDS, "run_id"
)

#: DepMap tables whose column names are exactly their model's field names, so
#: the schema can never drift behind the model without a column being missing.
DEPMAP_RELEASE_FIELDS = tuple(DepMapReleaseMetadata.model_fields)
DEPMAP_DATASET_FILE_FIELDS = tuple(DepMapDatasetFile.model_fields)
DATA_LICENSE_FIELDS = tuple(DataLicenseMetadata.model_fields)
DEPMAP_GENE_IDENTITY_FIELDS = tuple(DepMapGeneIdentity.model_fields)
DEPMAP_MODEL_FIELDS = tuple(DepMapModelMetadata.model_fields)
DEPMAP_OBSERVATION_FIELDS = tuple(DepMapDependencyObservation.model_fields)
DEPMAP_ANNOTATION_FIELDS = tuple(CommonEssentialAnnotation.model_fields)

#: The one list-valued DepMap column is stored as canonical JSON text.
_DEPMAP_JSON_FIELDS = frozenset({"quality_flags"})

_DEPMAP_RELEASE_UPSERT = _upsert_sql(
    "depmap_release", DEPMAP_RELEASE_FIELDS, "release_id"
)
_DEPMAP_DATASET_FILE_UPSERT = _upsert_sql(
    "depmap_dataset_file", DEPMAP_DATASET_FILE_FIELDS, "dataset_file_id"
)
_DATA_LICENSE_UPSERT = _upsert_sql(
    "data_license_metadata", DATA_LICENSE_FIELDS, "release_id"
)
_DEPMAP_GENE_IDENTITY_UPSERT = _upsert_sql(
    "depmap_gene_identity", DEPMAP_GENE_IDENTITY_FIELDS, "evidence_id"
)
_DEPMAP_MODEL_UPSERT = _upsert_sql(
    "depmap_model_metadata", DEPMAP_MODEL_FIELDS, "release_id, model_id"
)
_DEPMAP_OBSERVATION_UPSERT = _upsert_sql(
    "depmap_dependency_observation",
    DEPMAP_OBSERVATION_FIELDS,
    "observation_id",
)
_DEPMAP_ANNOTATION_UPSERT = _upsert_sql(
    "depmap_common_essential_annotation",
    DEPMAP_ANNOTATION_FIELDS,
    "evidence_id, annotation_type",
)

#: Run-to-release and run-to-file associations are written even for a run whose
#: queries all returned ``NO_RESULTS``, so an empty answer stays traceable to the
#: exact validated release bundle it was empty in.
_RUN_RELEASE_LINK = (
    _insert_sql("depmap_run_release", ("run_id", "release_id"))
    + " ON CONFLICT(run_id, release_id) DO NOTHING"
)
_RUN_DATASET_FILE_LINK = (
    _insert_sql("depmap_run_dataset_file", ("run_id", "dataset_file_id"))
    + " ON CONFLICT(run_id, dataset_file_id) DO NOTHING"
)


def _check_on_conflict(on_conflict: str) -> None:
    if on_conflict not in ("replace", "error"):
        raise ValueError(
            f"on_conflict must be 'replace' or 'error', received {on_conflict!r}"
        )


def _serialize_evidence(
    record: EvidenceRecord, project_name: str | None
) -> dict[str, object]:
    """Map an ``EvidenceRecord`` onto SQLite parameters, preserving every value."""
    params: dict[str, object] = {}
    for name in EVIDENCE_FIELDS:
        value = getattr(record, name)
        if name == "quality_flags":
            params[name] = json.dumps(list(value), ensure_ascii=False)
        elif name == "retrieved_at":
            params[name] = value.isoformat() if value is not None else None
        elif isinstance(value, Enum):
            params[name] = value.value
        else:
            params[name] = value
    params["project_name"] = project_name
    return params


def _deserialize_evidence(row: sqlite3.Row) -> dict[str, object]:
    """Inverse of :func:`_serialize_evidence`."""
    data: dict[str, object] = {}
    for name in EVIDENCE_FIELDS:
        value = row[name]
        if name == "quality_flags":
            data[name] = json.loads(value)
        elif name == "retrieved_at":
            data[name] = (
                datetime.fromisoformat(value) if value is not None else None
            )
        else:
            data[name] = value
    return data


def _serialize_project(project: Project) -> dict[str, object]:
    params: dict[str, object] = {}
    for name in PROJECT_FIELDS:
        value = getattr(project, name)
        if name == "candidate_genes":
            params[name] = json.dumps(list(value), ensure_ascii=False)
        else:
            params[name] = value
    return params


def _deserialize_project(row: sqlite3.Row) -> Project:
    return Project(
        project_name=row["project_name"],
        organism=row["organism"],
        disease=row["disease"],
        tissue=row["tissue"],
        phenotype=row["phenotype"],
        drug_or_treatment=row["drug_or_treatment"],
        research_question=row["research_question"],
        candidate_genes=json.loads(row["candidate_genes"]),
    )


def _serialize_publication(
    evidence_id: str, publication: PublicationMetadata
) -> dict[str, object]:
    if publication.source_identifier is None:
        raise EvidenceStorageError(
            f"publication metadata for {evidence_id!r} has no PMID"
        )
    return {
        "evidence_id": evidence_id,
        "pmid": publication.source_identifier,
        "title": publication.title,
        "abstract": publication.abstract,
        "journal": publication.journal,
        "publication_date": publication.publication_date,
        "publication_year": publication.publication_year,
        "authors": json.dumps(publication.authors, ensure_ascii=False),
        "publication_types": json.dumps(
            publication.publication_types, ensure_ascii=False
        ),
        "doi": publication.doi,
        "language": publication.language,
    }


def _deserialize_publication(row: sqlite3.Row) -> PublicationMetadata:
    return PublicationMetadata(
        source_identifier=row["pmid"],
        title=row["title"],
        abstract=row["abstract"],
        journal=row["journal"],
        publication_date=row["publication_date"],
        publication_year=row["publication_year"],
        authors=json.loads(row["authors"]),
        publication_types=json.loads(row["publication_types"]),
        doi=row["doi"],
        language=row["language"],
    )


def _serialize_query_match(match: QueryMatch) -> dict[str, object]:
    data = match.model_dump()
    data["retrieved_at"] = match.retrieved_at.isoformat()
    return data


def _deserialize_query_match(row: sqlite3.Row) -> QueryMatch:
    return QueryMatch(
        **{
            name: (
                datetime.fromisoformat(row[name])
                if name == "retrieved_at"
                else row[name]
            )
            for name in QUERY_MATCH_FIELDS
        }
    )


def _is_datetime_field(model_class: type[BaseModel], name: str) -> bool:
    """Whether a model field holds a ``datetime`` (optionally nullable)."""
    annotation = model_class.model_fields[name].annotation
    return annotation is datetime or datetime in get_args(annotation)


def _serialize_depmap(
    model: BaseModel,
    fields: Sequence[str],
    *,
    json_fields: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Map a DepMap model onto SQLite parameters, preserving every value.

    ``None`` becomes SQL ``NULL``; enum members become their declared values;
    the one list-valued column becomes canonical JSON; timestamps keep their
    offset and microseconds.
    """
    params: dict[str, object] = {}
    for name in fields:
        value = getattr(model, name)
        if name in json_fields:
            params[name] = json.dumps(list(value), ensure_ascii=False)
        elif isinstance(value, datetime):
            params[name] = value.isoformat()
        elif isinstance(value, Enum):
            params[name] = value.value
        else:
            params[name] = value
    return params


def _deserialize_depmap(
    row: sqlite3.Row,
    model_class: type[BaseModel],
    fields: Sequence[str],
    *,
    json_fields: frozenset[str] = frozenset(),
) -> BaseModel:
    """Inverse of :func:`_serialize_depmap`.

    A ``NULL`` column reads back as ``None`` — for a dependency observation that
    is the point: a missing measurement must never come back as a number.
    """
    model_fields = tuple(model_class.model_fields)
    data: dict[str, object] = {}
    for name in fields:
        value = row[name]
        if name in json_fields:
            data[name] = json.loads(value)
        elif name in model_fields and _is_datetime_field(model_class, name):
            data[name] = (
                datetime.fromisoformat(value) if value is not None else None
            )
        else:
            data[name] = value
    return model_class(**data)


class EvidenceDatabase(DecisionStorageMixin, InterpretationStorageMixin):
    """A local SQLite store for ``Project`` and ``EvidenceRecord`` rows."""

    def __init__(self, path: str | Path | None = None) -> None:
        """Open (creating if needed) a database; ``None`` means in-memory."""
        self._path = ":memory:" if path is None else str(path)
        if self._path != ":memory:":
            parent = Path(self._path).parent
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self.initialize()

    # -- lifecycle ---------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def initialize(self) -> None:
        """Create or migrate the schema. Safe to call repeatedly.

        Migrations run before the shared schema script, so a database that
        predates a table is upgraded one version at a time: a v1 database is
        rebuilt into the v2 shape first, then v2 gains the v3 DepMap factual
        tables, then v3 gains the v4 Interpretation tables, and v4 gains the
        v5 external-provider provenance tables, v6 usage, v7 manual provenance
        and v8 decision storage. Existing rows,
        project links, enum values and SQL ``NULL``
        values are preserved at every step, and no migration infers a value the
        source never supplied.
        """
        self._connection.executescript(_SCHEMA_METADATA_SQL)
        row = self._connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        stored_version: int | None = None
        if row is not None:
            try:
                stored_version = int(row["value"])
            except (TypeError, ValueError) as exc:
                raise EvidenceStorageError(
                    f"invalid SQLite schema version {row['value']!r}"
                ) from exc

        if stored_version == 1:
            self._migrate_v1_to_v2()
            stored_version = 2
        if stored_version == 2:
            self._migrate_v2_to_v3()
            stored_version = 3
        if stored_version == 3:
            self._migrate_v3_to_v4()
            stored_version = 4
        if stored_version == 4:
            self._migrate_v4_to_v5()
            stored_version = 5
        if stored_version == 5:
            self._migrate_v5_to_v6()
            stored_version = 6
        if stored_version == 6:
            self._connection.executescript(MANUAL_BRIDGE_SCHEMA_SQL)
            self._connection.execute("UPDATE schema_metadata SET value='7' WHERE key='schema_version'")
            self._connection.commit()
            stored_version = 7
        if stored_version == 7:
            self._connection.executescript(DECISION_SCHEMA_SQL)
            self._connection.execute("UPDATE schema_metadata SET value='8' WHERE key='schema_version'")
            self._connection.commit()
            stored_version = 8
        if stored_version not in (None, SCHEMA_VERSION):
            raise EvidenceStorageError(
                f"unsupported SQLite schema version {stored_version};"
                f" expected {SCHEMA_VERSION}"
            )

        self._connection.executescript(_SCHEMA_SQL)
        self._connection.executescript(MANUAL_BRIDGE_SCHEMA_SQL)
        self._connection.executescript(DECISION_SCHEMA_SQL)
        if stored_version is None:
            self._connection.execute(
                "INSERT INTO schema_metadata (key, value) VALUES"
                " ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self._connection.commit()

    def _migrate_v1_to_v2(self) -> None:
        """Make species nullable and add public-evidence provenance tables."""
        self._connection.commit()
        self._connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self._connection.executescript(_MIGRATE_V1_TO_V2_SQL)
            self._connection.execute(
                "UPDATE schema_metadata SET value = '2'"
                " WHERE key = 'schema_version'",
            )
            self._connection.commit()
        finally:
            self._connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_v2_to_v3(self) -> None:
        """Add the DepMap factual collection tables.

        The upgrade is purely additive: no existing table is rebuilt, renamed or
        rewritten, so every v2 row — including SQL ``NULL`` values — survives
        unchanged.
        """
        self._connection.executescript(_DEPMAP_SCHEMA_SQL)
        self._connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            ("3",),
        )
        self._connection.commit()

    def _migrate_v3_to_v4(self) -> None:
        """Add interpretation tables without rewriting factual v1/v2/v3 rows."""
        self._connection.executescript(INTERPRETATION_SCHEMA_SQL)
        self._connection.execute(
            "UPDATE schema_metadata SET value = '4' WHERE key = 'schema_version'"
        )
        self._connection.commit()

    def _migrate_v4_to_v5(self) -> None:
        """Add authorization and execution provenance without rewriting facts."""
        self._connection.executescript(EXTERNAL_PROVIDER_SCHEMA_SQL)
        self._connection.execute(
            "UPDATE schema_metadata SET value = '5' WHERE key = 'schema_version'"
        )
        self._connection.commit()

    def _migrate_v5_to_v6(self) -> None:
        """Add optional provider-reported reasoning usage without rewriting runs."""
        columns = {row["name"] for row in self._connection.execute(
            "PRAGMA table_info(provider_usage)"
        )}
        if "reasoning_units" not in columns:
            self._connection.execute(
                "ALTER TABLE provider_usage ADD COLUMN reasoning_units REAL"
                " CHECK (reasoning_units IS NULL OR reasoning_units >= 0)"
            )
        self._connection.execute(
            "UPDATE schema_metadata SET value = '6' WHERE key = 'schema_version'"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EvidenceDatabase":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- projects ----------------------------------------------------------

    def save_project(self, project: Project, *, on_conflict: OnConflict = "replace") -> None:
        """Persist a project. ``replace`` updates an existing row in place."""
        _check_on_conflict(on_conflict)
        sql = _PROJECT_UPSERT if on_conflict == "replace" else _PROJECT_INSERT
        try:
            with self._connection:
                self._connection.execute(sql, _serialize_project(project))
        except sqlite3.IntegrityError as exc:
            raise EvidenceStorageError(
                f"project {project.project_name!r} is already stored; pass"
                " on_conflict='replace' to overwrite it"
            ) from exc

    def get_project(self, project_name: str) -> Project | None:
        row = self._connection.execute(
            "SELECT * FROM project WHERE project_name = ?", (project_name,)
        ).fetchone()
        return None if row is None else _deserialize_project(row)

    def list_projects(self) -> list[Project]:
        rows = self._connection.execute(
            "SELECT * FROM project ORDER BY project_name"
        ).fetchall()
        return [_deserialize_project(row) for row in rows]

    # -- evidence ----------------------------------------------------------

    def save_evidence(
        self,
        record: EvidenceRecord,
        *,
        project_name: str | None = None,
        on_conflict: OnConflict = "replace",
    ) -> None:
        """Persist one evidence record, optionally linked to a stored project."""
        self.save_evidence_batch(
            [record], project_name=project_name, on_conflict=on_conflict
        )

    def save_evidence_batch(
        self,
        records: Iterable[EvidenceRecord],
        *,
        project_name: str | None = None,
        on_conflict: OnConflict = "replace",
    ) -> int:
        """Persist records in one transaction and return how many were written.

        A repeated ``evidence_id`` *inside* this call is always rejected; the
        ``on_conflict`` policy only governs records already stored from an
        earlier call.
        """
        _check_on_conflict(on_conflict)
        batch = list(records)
        _reject_repeated_ids(batch)

        sql = _EVIDENCE_UPSERT if on_conflict == "replace" else _EVIDENCE_INSERT
        try:
            with self._connection:
                for record in batch:
                    self._connection.execute(
                        sql, _serialize_evidence(record, project_name)
                    )
        except sqlite3.IntegrityError as exc:
            raise _translate_integrity_error(
                exc,
                project_name=project_name,
                evidence_id=batch[0].evidence_id if batch else None,
                on_conflict=on_conflict,
            ) from exc
        return len(batch)

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        row = self._connection.execute(
            "SELECT * FROM evidence_record WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        return None if row is None else EvidenceRecord.model_validate(
            _deserialize_evidence(row)
        )

    def list_evidence(
        self, *, project_name: str | None = None, gene: str | None = None
    ) -> list[EvidenceRecord]:
        """Return records filtered by linked project and/or gene name."""
        clauses: list[str] = []
        parameters: list[object] = []
        if project_name is not None:
            clauses.append("project_name = ?")
            parameters.append(project_name)
        if gene is not None:
            clauses.append("gene = ?")
            parameters.append(gene)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM evidence_record{where} ORDER BY evidence_id",
            parameters,
        ).fetchall()
        return [
            EvidenceRecord.model_validate(_deserialize_evidence(row)) for row in rows
        ]

    def count_evidence(self, *, project_name: str | None = None) -> int:
        if project_name is None:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM evidence_record"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM evidence_record WHERE project_name = ?",
                (project_name,),
            ).fetchone()
        return int(row["total"])

    # -- public-source provenance -----------------------------------------

    def save_connector_run(
        self,
        run: ConnectorRunResult,
        *,
        project_name: str | None = None,
        on_conflict: OnConflict = "replace",
    ) -> int:
        """Persist a connector run, records, metadata, and exact query links.

        All rows are written in one transaction. Publication data and query
        provenance stay relational and queryable rather than being serialized
        into ``EvidenceRecord``.

        A DepMap run is refused here: it carries release, file, model,
        measurement and annotation provenance that this method would silently
        drop. Use :meth:`save_depmap_run` instead.
        """
        if isinstance(run, DepMapRunResult):
            raise EvidenceStorageError(
                f"run {run.run_id!r} is a DepMap run and must be saved with"
                " save_depmap_run(); saving it here would drop its release,"
                " file, model and measurement provenance"
            )
        _check_on_conflict(on_conflict)
        _reject_repeated_ids(run.records)
        evidence_sql = (
            _EVIDENCE_UPSERT if on_conflict == "replace" else _EVIDENCE_INSERT
        )
        try:
            with self._connection:
                for record in run.records:
                    self._connection.execute(
                        evidence_sql, _serialize_evidence(record, project_name)
                    )
                self._connection.execute(
                    _CONNECTOR_RUN_UPSERT,
                    {
                        "run_id": run.run_id,
                        "connector": run.connector,
                        "source_scope": run.source_scope.value,
                        "retrieved_at": run.retrieved_at.isoformat(),
                    },
                )
                for evidence_id, publication in run.publications.items():
                    self._connection.execute(
                        _PUBLICATION_UPSERT,
                        _serialize_publication(evidence_id, publication),
                    )
                for match in run.query_match_records():
                    self._connection.execute(
                        _QUERY_MATCH_UPSERT, _serialize_query_match(match)
                    )
        except sqlite3.IntegrityError as exc:
            raise EvidenceStorageError(
                f"SQLite rejected connector run {run.run_id!r}: {exc}"
            ) from exc
        return len(run.records)

    def get_publication_metadata(
        self, evidence_id: str
    ) -> PublicationMetadata | None:
        row = self._connection.execute(
            "SELECT * FROM publication_metadata WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return None if row is None else _deserialize_publication(row)

    def list_query_matches(
        self,
        *,
        evidence_id: str | None = None,
        run_id: str | None = None,
    ) -> list[QueryMatch]:
        clauses: list[str] = []
        parameters: list[object] = []
        if evidence_id is not None:
            clauses.append("evidence_id = ?")
            parameters.append(evidence_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT * FROM query_match"
            f"{where} ORDER BY retrieved_at, evidence_id, query_text",
            parameters,
        ).fetchall()
        return [_deserialize_query_match(row) for row in rows]

    # -- DepMap factual collection ----------------------------------------

    def save_depmap_run(
        self,
        run: DepMapRunResult,
        *,
        project_name: str | None = None,
        on_conflict: OnConflict = "replace",
    ) -> int:
        """Persist a DepMap run, its records, and its factual source rows.

        Everything is written in one transaction, so a validation or storage
        failure leaves no partial profile: either the whole run is stored or
        nothing is. Every identity is deterministic, so re-importing the same
        release/file/profile upserts instead of duplicating observations.

        Two integrity conflicts are refused rather than overwritten, and are
        reported as different failures: a *source-provided* checksum that
        disagrees with the stored one is an official-checksum conflict, while a
        changed *locally computed* hash is a local-file integrity mismatch. A
        local hash is never described as official checksum validation.
        """
        _check_on_conflict(on_conflict)
        _reject_repeated_ids(run.records)
        evidence_sql = (
            _EVIDENCE_UPSERT if on_conflict == "replace" else _EVIDENCE_INSERT
        )
        try:
            with self._connection:
                for record in run.records:
                    self._connection.execute(
                        evidence_sql, _serialize_evidence(record, project_name)
                    )
                self._connection.execute(
                    _CONNECTOR_RUN_UPSERT,
                    {
                        "run_id": run.run_id,
                        "connector": run.connector,
                        "source_scope": run.source_scope.value,
                        "retrieved_at": run.retrieved_at.isoformat(),
                    },
                )
                for evidence_id, publication in run.publications.items():
                    self._connection.execute(
                        _PUBLICATION_UPSERT,
                        _serialize_publication(evidence_id, publication),
                    )
                for match in run.query_match_records():
                    self._connection.execute(
                        _QUERY_MATCH_UPSERT, _serialize_query_match(match)
                    )

                if run.release is not None:
                    self._assert_depmap_integrity(run)
                    self._connection.execute(
                        _DEPMAP_RELEASE_UPSERT,
                        _serialize_depmap(run.release, DEPMAP_RELEASE_FIELDS),
                    )
                    if run.license_metadata is not None:
                        self._connection.execute(
                            _DATA_LICENSE_UPSERT,
                            _serialize_depmap(
                                run.license_metadata, DATA_LICENSE_FIELDS
                            ),
                        )
                    for dataset_file in run.dataset_files:
                        self._connection.execute(
                            _DEPMAP_DATASET_FILE_UPSERT,
                            _serialize_depmap(
                                dataset_file, DEPMAP_DATASET_FILE_FIELDS
                            ),
                        )
                        self._connection.execute(
                            _RUN_DATASET_FILE_LINK,
                            {
                                "run_id": run.run_id,
                                "dataset_file_id": dataset_file.dataset_file_id,
                            },
                        )
                    self._connection.execute(
                        _RUN_RELEASE_LINK,
                        {
                            "run_id": run.run_id,
                            "release_id": run.release.release_id,
                        },
                    )

                    seen_models: set[tuple[str, str]] = set()
                    for profile in run.depmap_profiles.values():
                        self._connection.execute(
                            _DEPMAP_GENE_IDENTITY_UPSERT,
                            _serialize_depmap(
                                profile.gene_identity, DEPMAP_GENE_IDENTITY_FIELDS
                            ),
                        )
                        for model in profile.models:
                            key = (model.release_id, model.model_id)
                            if key in seen_models:
                                # Model metadata is shared across genes, not
                                # re-written once per queried gene.
                                continue
                            seen_models.add(key)
                            self._connection.execute(
                                _DEPMAP_MODEL_UPSERT,
                                _serialize_depmap(model, DEPMAP_MODEL_FIELDS),
                            )
                        for observation in profile.observations:
                            self._connection.execute(
                                _DEPMAP_OBSERVATION_UPSERT,
                                _serialize_depmap(
                                    observation,
                                    DEPMAP_OBSERVATION_FIELDS,
                                    json_fields=_DEPMAP_JSON_FIELDS,
                                ),
                            )
                        for annotation in profile.common_essential_annotations:
                            self._connection.execute(
                                _DEPMAP_ANNOTATION_UPSERT,
                                _serialize_depmap(
                                    annotation, DEPMAP_ANNOTATION_FIELDS
                                ),
                            )
        except sqlite3.IntegrityError as exc:
            raise EvidenceStorageError(
                f"SQLite rejected DepMap run {run.run_id!r}: {exc}"
            ) from exc
        return len(run.records)

    def _assert_depmap_integrity(self, run: DepMapRunResult) -> None:
        """Refuse a release/file identity whose stored checksums no longer agree."""
        for dataset_file in run.dataset_files:
            row = self._connection.execute(
                "SELECT source_checksum_value, local_file_integrity_hash_value"
                " FROM depmap_dataset_file WHERE dataset_file_id = ?",
                (dataset_file.dataset_file_id,),
            ).fetchone()
            if row is None:
                continue
            stored_source = row["source_checksum_value"]
            incoming_source = dataset_file.source_checksum_value
            if (
                stored_source is not None
                and incoming_source is not None
                and stored_source != incoming_source
            ):
                raise EvidenceStorageError(
                    f"the checksum DepMap publishes for"
                    f" {dataset_file.dataset_name!r} of release"
                    f" {dataset_file.release_id!r} changed from {stored_source} to"
                    f" {incoming_source}; an official checksum conflict is an"
                    " integrity failure, not an overwrite"
                )
            stored_local = row["local_file_integrity_hash_value"]
            incoming_local = dataset_file.local_file_integrity_hash_value
            if (
                stored_local is not None
                and incoming_local is not None
                and stored_local != incoming_local
            ):
                raise EvidenceStorageError(
                    "the local file integrity hash"
                    f" ({dataset_file.local_file_integrity_hash_algorithm}) for"
                    f" {dataset_file.dataset_name!r} of release"
                    f" {dataset_file.release_id!r} changed from {stored_local} to"
                    f" {incoming_local}; this is a local-file integrity mismatch,"
                    " not an official checksum conflict"
                )

    def get_depmap_release(self, release_id: str) -> DepMapReleaseMetadata | None:
        row = self._connection.execute(
            "SELECT * FROM depmap_release WHERE release_id = ?", (release_id,)
        ).fetchone()
        return (
            None
            if row is None
            else _deserialize_depmap(row, DepMapReleaseMetadata, DEPMAP_RELEASE_FIELDS)
        )

    def list_depmap_releases(self) -> list[DepMapReleaseMetadata]:
        rows = self._connection.execute(
            "SELECT * FROM depmap_release ORDER BY release_name"
        ).fetchall()
        return [
            _deserialize_depmap(row, DepMapReleaseMetadata, DEPMAP_RELEASE_FIELDS)
            for row in rows
        ]

    def list_depmap_dataset_files(
        self, release_id: str | None = None
    ) -> list[DepMapDatasetFile]:
        if release_id is None:
            rows = self._connection.execute(
                "SELECT * FROM depmap_dataset_file ORDER BY dataset_file_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM depmap_dataset_file WHERE release_id = ?"
                " ORDER BY dataset_file_id",
                (release_id,),
            ).fetchall()
        return [
            _deserialize_depmap(row, DepMapDatasetFile, DEPMAP_DATASET_FILE_FIELDS)
            for row in rows
        ]

    def get_data_license_metadata(
        self, release_id: str
    ) -> DataLicenseMetadata | None:
        row = self._connection.execute(
            "SELECT * FROM data_license_metadata WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        return (
            None
            if row is None
            else _deserialize_depmap(row, DataLicenseMetadata, DATA_LICENSE_FIELDS)
        )

    def get_depmap_gene_identity(
        self, evidence_id: str
    ) -> DepMapGeneIdentity | None:
        row = self._connection.execute(
            "SELECT * FROM depmap_gene_identity WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return (
            None
            if row is None
            else _deserialize_depmap(
                row, DepMapGeneIdentity, DEPMAP_GENE_IDENTITY_FIELDS
            )
        )

    def list_depmap_model_metadata(
        self, *, release_id: str | None = None, model_ids: Sequence[str] | None = None
    ) -> list[DepMapModelMetadata]:
        clauses: list[str] = []
        parameters: list[object] = []
        if release_id is not None:
            clauses.append("release_id = ?")
            parameters.append(release_id)
        if model_ids is not None:
            if not model_ids:
                return []
            placeholders = ", ".join("?" for _ in model_ids)
            clauses.append(f"model_id IN ({placeholders})")
            parameters.extend(model_ids)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT * FROM depmap_model_metadata"
            f"{where} ORDER BY release_id, model_id",
            parameters,
        ).fetchall()
        return [
            _deserialize_depmap(row, DepMapModelMetadata, DEPMAP_MODEL_FIELDS)
            for row in rows
        ]

    def list_depmap_observations(
        self,
        *,
        evidence_id: str | None = None,
        release_id: str | None = None,
        model_id: str | None = None,
        measurement_type: object | None = None,
    ) -> list[DepMapDependencyObservation]:
        """Read measurement rows back, filters being fact-based only."""
        clauses: list[str] = []
        parameters: list[object] = []
        if evidence_id is not None:
            clauses.append("evidence_id = ?")
            parameters.append(evidence_id)
        if release_id is not None:
            clauses.append("release_id = ?")
            parameters.append(release_id)
        if model_id is not None:
            clauses.append("model_id = ?")
            parameters.append(model_id)
        if measurement_type is not None:
            clauses.append("measurement_type = ?")
            parameters.append(getattr(measurement_type, "value", measurement_type))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT * FROM depmap_dependency_observation"
            f"{where} ORDER BY dataset_name, model_id, observation_id",
            parameters,
        ).fetchall()
        return [
            _deserialize_depmap(
                row,
                DepMapDependencyObservation,
                DEPMAP_OBSERVATION_FIELDS,
                json_fields=_DEPMAP_JSON_FIELDS,
            )
            for row in rows
        ]

    def list_common_essential_annotations(
        self,
        *,
        evidence_id: str | None = None,
        release_id: str | None = None,
    ) -> list[CommonEssentialAnnotation]:
        clauses: list[str] = []
        parameters: list[object] = []
        if evidence_id is not None:
            clauses.append("evidence_id = ?")
            parameters.append(evidence_id)
        if release_id is not None:
            clauses.append("release_id = ?")
            parameters.append(release_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT * FROM depmap_common_essential_annotation"
            f"{where} ORDER BY evidence_id, annotation_type",
            parameters,
        ).fetchall()
        return [
            _deserialize_depmap(
                row, CommonEssentialAnnotation, DEPMAP_ANNOTATION_FIELDS
            )
            for row in rows
        ]

    def list_depmap_run_releases(
        self, *, run_id: str | None = None
    ) -> list[tuple[str, str]]:
        """The (run_id, release_id) associations this run recorded."""
        if run_id is None:
            rows = self._connection.execute(
                "SELECT run_id, release_id FROM depmap_run_release"
                " ORDER BY run_id, release_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT run_id, release_id FROM depmap_run_release"
                " WHERE run_id = ? ORDER BY release_id",
                (run_id,),
            ).fetchall()
        return [(row["run_id"], row["release_id"]) for row in rows]

    def list_depmap_run_dataset_files(
        self, *, run_id: str | None = None
    ) -> list[tuple[str, str]]:
        """The (run_id, dataset_file_id) associations this run recorded."""
        if run_id is None:
            rows = self._connection.execute(
                "SELECT run_id, dataset_file_id FROM depmap_run_dataset_file"
                " ORDER BY run_id, dataset_file_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT run_id, dataset_file_id FROM depmap_run_dataset_file"
                " WHERE run_id = ? ORDER BY dataset_file_id",
                (run_id,),
            ).fetchall()
        return [(row["run_id"], row["dataset_file_id"]) for row in rows]

    def count_depmap_observations(
        self, *, evidence_id: str | None = None
    ) -> int:
        if evidence_id is None:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM depmap_dependency_observation"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM depmap_dependency_observation"
                " WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        return int(row["total"])


def _reject_repeated_ids(records: Sequence[EvidenceRecord]) -> None:
    seen: set[str] = set()
    for record in records:
        if record.evidence_id in seen:
            raise DuplicateEvidenceError(
                f"duplicate evidence_id {record.evidence_id!r} within a single"
                " batch; refuse to let one record overwrite another",
                field="evidence_id",
                value=record.evidence_id,
            )
        seen.add(record.evidence_id)


def _translate_integrity_error(
    exc: sqlite3.IntegrityError,
    *,
    project_name: str | None,
    evidence_id: str | None,
    on_conflict: str,
) -> Exception:
    detail = str(exc)
    if "evidence_record.evidence_id" in detail:
        return DuplicateEvidenceError(
            f"evidence_id {evidence_id!r} is already stored; pass"
            " on_conflict='replace' to overwrite it",
            field="evidence_id",
            value=evidence_id,
        )
    if "FOREIGN KEY" in detail:
        return EvidenceStorageError(
            f"project {project_name!r} is not stored yet; save the Project before"
            " linking evidence to it"
        )
    return EvidenceStorageError(
        f"SQLite rejected the write (on_conflict={on_conflict!r}): {detail}"
    )
