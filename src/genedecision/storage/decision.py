"""Additive decision storage, immutable terminal results and exact provenance."""

import sqlite3
from types import SimpleNamespace

from genedecision.errors import EvidenceStorageError
from genedecision.models.decision_core import (
    DecisionInput, DecisionReport, DecisionRun, DecisionRunStatus,
    canonical_decision_snapshot, validate_decision_assessment,
)


DECISION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decision_run (
    run_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL REFERENCES project(project_name) ON DELETE CASCADE,
    hypothesis_id TEXT NOT NULL REFERENCES hypothesis(hypothesis_id) ON DELETE CASCADE,
    provider_json TEXT NOT NULL,
    input_snapshot TEXT NOT NULL,
    input_snapshot_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('STARTED','SUCCEEDED','FAILED')),
    error TEXT,
    run_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_run_project ON decision_run(project_name, hypothesis_id);
CREATE INDEX IF NOT EXISTS idx_decision_run_snapshot ON decision_run(input_snapshot_hash);
CREATE TRIGGER IF NOT EXISTS decision_run_no_terminal_update BEFORE UPDATE ON decision_run
WHEN OLD.status IN ('SUCCEEDED','FAILED')
BEGIN SELECT RAISE(ABORT, 'terminal decision runs are immutable'); END;

CREATE TABLE IF NOT EXISTS decision_selection (
    run_id TEXT NOT NULL REFERENCES decision_run(run_id) ON DELETE CASCADE,
    interpretation_id TEXT NOT NULL REFERENCES evidence_interpretation(interpretation_id) ON DELETE CASCADE,
    interpretation_run_id TEXT NOT NULL REFERENCES interpretation_run(run_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL,
    source_scope TEXT NOT NULL CHECK (source_scope IN ('USER','PUBLIC')),
    source_kind TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    PRIMARY KEY (run_id, interpretation_id)
);
CREATE TRIGGER IF NOT EXISTS decision_selection_requires_success BEFORE INSERT ON decision_selection
WHEN NOT EXISTS (
    SELECT 1 FROM evidence_interpretation i JOIN interpretation_run r ON r.run_id=i.run_id
    JOIN decision_run d ON d.run_id=NEW.run_id
    WHERE i.interpretation_id=NEW.interpretation_id AND r.run_id=NEW.interpretation_run_id
      AND r.status='SUCCEEDED' AND d.status='STARTED' AND i.evidence_id=NEW.evidence_id
      AND i.project_name=d.project_name AND i.hypothesis_id=d.hypothesis_id
)
BEGIN SELECT RAISE(ABORT, 'decision selection requires matching successful interpretation'); END;

CREATE TABLE IF NOT EXISTS decision_report (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES decision_run(run_id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (decision IN ('PRIORITIZE','COMPUTE_MORE','DEPRIORITIZE')),
    report_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS decision_report_requires_success BEFORE INSERT ON decision_report
WHEN NOT EXISTS (SELECT 1 FROM decision_run WHERE run_id=NEW.run_id AND status='SUCCEEDED')
BEGIN SELECT RAISE(ABORT, 'decision report requires successful decision run'); END;

CREATE TABLE IF NOT EXISTS decision_axis_assessment (
    report_id TEXT NOT NULL REFERENCES decision_report(report_id) ON DELETE CASCADE,
    axis TEXT NOT NULL CHECK (axis IN ('INTERNAL_EVIDENCE','INDEPENDENT_EVIDENCE','CAUSAL_EVIDENCE',
      'BIOLOGICAL_PLAUSIBILITY','NOVELTY','CONTRADICTIONS_RISK','INFORMATION_GAP')),
    status TEXT NOT NULL CHECK (status IN ('STRONG','MODERATE','WEAK','ABSENT','MIXED','UNKNOWN')),
    assessment_json TEXT NOT NULL,
    PRIMARY KEY (report_id, axis)
);
CREATE TABLE IF NOT EXISTS decision_red_team_finding (
    report_id TEXT NOT NULL REFERENCES decision_report(report_id) ON DELETE CASCADE,
    finding_id TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    unresolved INTEGER NOT NULL CHECK (unresolved IN (0,1)),
    finding_json TEXT NOT NULL,
    PRIMARY KEY (report_id, finding_id)
);
CREATE TABLE IF NOT EXISTS decision_information_gap (
    report_id TEXT NOT NULL REFERENCES decision_report(report_id) ON DELETE CASCADE,
    gap_id TEXT NOT NULL,
    route TEXT NOT NULL CHECK (route IN ('COMPUTATIONAL_PUBLIC','WET_LAB')),
    priority TEXT NOT NULL,
    actionable INTEGER NOT NULL CHECK (actionable IN (0,1)),
    gap_json TEXT NOT NULL,
    PRIMARY KEY (report_id, gap_id)
);
CREATE TABLE IF NOT EXISTS decision_next_analysis (
    report_id TEXT PRIMARY KEY REFERENCES decision_report(report_id) ON DELETE CASCADE,
    analysis_json TEXT NOT NULL
);
"""

for _table in ("decision_selection", "decision_report", "decision_axis_assessment",
               "decision_red_team_finding", "decision_information_gap", "decision_next_analysis"):
    DECISION_SCHEMA_SQL += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_update BEFORE UPDATE ON {_table}
BEGIN SELECT RAISE(ABORT, 'decision records are immutable'); END;
"""


class DecisionStorageMixin:
    def start_decision_run(self, run: DecisionRun):
        run = DecisionRun.model_validate(run)
        if run.status is not DecisionRunStatus.STARTED:
            raise EvidenceStorageError("new decision run must be STARTED")
        data = DecisionInput.model_validate_json(run.input_snapshot)
        # Rebuild through the same selection gate, never trust caller-created bundles.
        from genedecision.decision.core import DecisionService
        rebuilt = DecisionService(self).prepare(project_name=data.project_name, gene=data.gene,
            hypothesis_id=data.hypothesis.hypothesis_id,
            interpretation_ids=tuple(s.interpretation.interpretation_id for s in data.selected),
            provider=SimpleNamespace(identity=data.provider), factual_context=data.factual_context,
            coverage_audits=data.coverage_audits, grounded_risk_questions=data.grounded_risk_questions)
        if canonical_decision_snapshot(rebuilt) != (run.input_snapshot, run.input_snapshot_hash):
            raise EvidenceStorageError("decision input differs from persisted selection")
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO decision_run (run_id,project_name,hypothesis_id,provider_json,"
                    "input_snapshot,input_snapshot_hash,started_at,status,run_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (run.run_id, run.project_name, run.hypothesis_id, run.provider.model_dump_json(),
                     run.input_snapshot, run.input_snapshot_hash, run.started_at.isoformat(),
                     run.status.value, run.model_dump_json()))
                for selected in data.selected:
                    self._connection.execute("INSERT INTO decision_selection VALUES (?,?,?,?,?,?,?)",
                        (run.run_id, selected.interpretation.interpretation_id, selected.run_id,
                         selected.provenance.evidence_id, selected.provenance.source_scope.value,
                         selected.provenance.source_kind.value, selected.model_dump_json()))
        except sqlite3.IntegrityError as exc:
            raise EvidenceStorageError("decision identity is duplicate or invalid") from exc
        return run

    def finish_decision_run(self, run: DecisionRun, report: DecisionReport | None):
        run = DecisionRun.model_validate(run)
        report = DecisionReport.model_validate(report) if report is not None else None
        old = self.get_decision_run(run.run_id)
        if old is None or old.status is not DecisionRunStatus.STARTED or run.status is DecisionRunStatus.STARTED:
            raise EvidenceStorageError("decision run absent or already terminal")
        identity_fields = set(DecisionRun.model_fields) - {"completed_at", "latency_ms", "status", "error"}
        if any(getattr(run,f) != getattr(old,f) for f in identity_fields):
            raise EvidenceStorageError("decision run identity changed")
        if (run.status is DecisionRunStatus.SUCCEEDED) != (report is not None):
            raise EvidenceStorageError("decision result/lifecycle mismatch")
        data = DecisionInput.model_validate_json(old.input_snapshot)
        if report is not None:
            if (report.run_id != run.run_id or report.project_name != run.project_name
                or report.hypothesis_id != run.hypothesis_id or report.input_snapshot_hash != run.input_snapshot_hash
                or report.selected_interpretation_ids != tuple(s.interpretation.interpretation_id for s in data.selected)
                or report.created_at != run.completed_at):
                raise EvidenceStorageError("decision report provenance mismatch")
            validate_decision_assessment(data, report.assessment)
            if run.provider.provider_type.value == "LLM":
                from genedecision.decision.llm import MODE, LLMDecisionCandidate, validate_llm_candidate
                if (report.assessment.synthesis_label != MODE or
                        report.assessment.scientific_validation != "SCIENTIFICALLY_UNVALIDATED"):
                    raise EvidenceStorageError("manual report requires unvalidated execution labels")
                if (report.provider_candidate_json is None or validate_llm_candidate(data,
                        LLMDecisionCandidate.model_validate_json(report.provider_candidate_json)) != report.assessment):
                    raise EvidenceStorageError("manual candidate/report mismatch")
            elif report.provider_candidate_json is not None:
                raise EvidenceStorageError("non-LLM report cannot claim a provider candidate")
        try:
            with self._connection:
                changed = self._connection.execute("UPDATE decision_run SET completed_at=?,latency_ms=?,"
                    "status=?,error=?,run_json=? WHERE run_id=? AND status='STARTED'",
                    (run.completed_at.isoformat(), run.latency_ms, run.status.value, run.error,
                     run.model_dump_json(),run.run_id))
                if changed.rowcount != 1:
                    raise EvidenceStorageError("decision run already terminal")
                if report is not None:
                    a = report.assessment
                    self._connection.execute("INSERT INTO decision_report VALUES (?,?,?,?)",
                        (report.report_id, run.run_id, a.decision.value, report.model_dump_json()))
                    for axis in a.axes:
                        self._connection.execute("INSERT INTO decision_axis_assessment VALUES (?,?,?,?)",
                            (report.report_id,axis.axis.value,axis.status.value,axis.model_dump_json()))
                    for finding in a.red_team_findings:
                        self._connection.execute("INSERT INTO decision_red_team_finding VALUES (?,?,?,?,?,?)",
                            (report.report_id,finding.finding_id,finding.category.value,
                             finding.severity.value,int(finding.unresolved),finding.model_dump_json()))
                    for gap in a.information_gaps:
                        self._connection.execute("INSERT INTO decision_information_gap VALUES (?,?,?,?,?,?)",
                            (report.report_id,gap.gap_id,gap.route.value,gap.priority.value,
                             int(gap.actionable),gap.model_dump_json()))
                    if a.most_decision_changing_next_analysis:
                        self._connection.execute("INSERT INTO decision_next_analysis VALUES (?,?)",
                            (report.report_id,a.most_decision_changing_next_analysis.model_dump_json()))
        except sqlite3.IntegrityError as exc:
            raise EvidenceStorageError("decision persistence failed") from exc
        return run, report

    def get_decision_run(self, run_id):
        row = self._connection.execute("SELECT run_json FROM decision_run WHERE run_id=?",(run_id,)).fetchone()
        return DecisionRun.model_validate_json(row[0]) if row else None

    def get_decision_report(self, *, report_id=None, run_id=None):
        if (report_id is None) == (run_id is None):
            raise ValueError("specify exactly one decision report identity")
        key, value = ("report_id",report_id) if report_id is not None else ("run_id",run_id)
        row = self._connection.execute(f"SELECT report_json FROM decision_report WHERE {key}=?",(value,)).fetchone()
        return DecisionReport.model_validate_json(row[0]) if row else None

    def list_decision_runs(self, *, project_name=None):
        sql = "SELECT run_json FROM decision_run"
        params = ()
        if project_name is not None:
            sql += " WHERE project_name=?"
            params = (project_name,)
        return [DecisionRun.model_validate_json(r[0]) for r in self._connection.execute(sql+" ORDER BY started_at,run_id",params)]
