"""Entirely invented exploratory comparison workflow, using only existing public local APIs.

Run from the checkout: PYTHONPATH=src python examples/discovery_numeric_demo.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from genedecision.discovery import (
    ColumnContract, LocalMappingReference, MappingEntry, MeasurementAuditContract,
    NumericCondition, NumericContext, inspect_measurements, evaluate_numeric_conditions,
    load_numeric_condition, load_numeric_context, load_numeric_evaluation,
    replay_numeric_evaluation, verify_numeric_source,
)


def require(value):
    if not value:
        raise RuntimeError("SYNTHETIC_NUMERIC_DEMO_FAILED")


def main():
    print("SYNTHETIC ONLY: invented identifiers, measurements, threshold and QC declarations.")
    print("EXPLORATORY_ONLY; SCIENTIFICALLY_UNVERIFIED; scientific validation: NOT_PERFORMED.")
    print("Caller QC PASS/FAIL is not independent verification; ambiguous IDs stay ambiguous.")
    reference = LocalMappingReference.from_entries(
        reference_id="SYN_MAPPING", source_namespace="synthetic-source",
        target_namespace="synthetic-target", taxon="synthetic-taxon",
        entries=tuple(MappingEntry(source=s, target=t) for s, t in (
            ("SYN_AMBIGUOUS", "SYN_TARGET_A"), ("SYN_AMBIGUOUS", "SYN_TARGET_B"),
            ("SYN_FALSE", "SYN_TARGET_F"), ("SYN_UNKNOWN", "SYN_TARGET_U"),
            ("SYN_PASS", "SYN_TARGET_P"),
        )),
    )
    column = ColumnContract(
        primary_gene=dict(column="synthetic_feature", definition="Invented source ID"),
        measurements=[dict(column="synthetic_value", definition="Invented numerical observation")],
        namespace="synthetic-source", taxon="synthetic-taxon", mapping_reference=reference.binding,
    )
    audit_contract = MeasurementAuditContract.from_declarations(
        column_contract_sha256=column.content_sha256,
        policies=[dict(measurement_id="SYN_MEASURE", column="synthetic_value")],
        caller_context_text="Invented data; no scientific meaning established",
    )
    with TemporaryDirectory(prefix="genedecision-synthetic-numeric-") as temporary:
        path = Path(temporary) / "synthetic.csv"
        path.write_text("synthetic_feature,synthetic_value\nSYN_AMBIGUOUS,8\nSYN_FALSE,6\n"
                        "SYN_UNKNOWN,NA\nSYN_AMBIGUOUS,8\nSYN_PASS,8\n", encoding="utf-8")
        audit = inspect_measurements(path, column, audit_contract, mapping_reference=reference)
        source = dict(source_id="SYN_QC_SOURCE", release="SYN_RELEASE")
        context = NumericContext.from_declarations(
            context_id="SYN_CONTEXT", release="SYN_CONTEXT_RELEASE", measurement_audit_id=audit.audit_id,
            source_records=[dict(record_id="SYN_RECORD", **source, source_locator="SYN_INVENTED_LOCATOR",
                                 statement="Invented caller QC declarations")],
            qc_declarations=[
                dict(declaration_id="SYN_FAIL", status="DECLARED_FAIL", source_binding=source,
                     source_record_ids=["SYN_RECORD"], measurement_ids=["SYN_MEASURE"], scope="ROWS",
                     locators=[audit.rows[i].identifier_record.locator for i in (1, 2, 3)]),
                dict(declaration_id="SYN_PASS", status="DECLARED_PASS", source_binding=source,
                     source_record_ids=["SYN_RECORD"], measurement_ids=["SYN_MEASURE"], scope="ROWS",
                     locators=[audit.rows[4].identifier_record.locator]),
            ],
            tested_universe_text=None,
        )
        condition = NumericCondition.from_declarations(
            measurement_audit_id=audit.audit_id, context_sha256=context.content_sha256,
            predicates=[dict(predicate_id="SYN_GREATER", measurement_id="SYN_MEASURE", operator="GT",
                             threshold="7", provenance="Invented explicit comparison, no significance inference")],
            combine="ALL",
        )
        condition = load_numeric_condition(condition.model_dump_json())
        context = load_numeric_context(context.model_dump_json())
        result = evaluate_numeric_conditions(audit, condition, context=context)
        loaded = load_numeric_evaluation(result.model_dump_json())
        replay_numeric_evaluation(loaded, measurement_audit=audit, condition=condition,
                                  context=context, mapping_reference=reference)
        fresh = verify_numeric_source(path, loaded, mapping_reference=reference)
        require(fresh.evaluation_id == result.evaluation_id)
        require(result.counts.row_truth_counts == dict(T=3, F=1, U=1))
        require(result.counts.qc_flagged_row_count == 3 and len(result.qc_flagged_matches) == 1)
        require(result.counts.ambiguous_identity_row_count == result.counts.raw_duplicate_row_count == 2)
        require(all(r.scientific_hit_status == "NOT_CONFIRMED" for r in result.rows))
        print(result.safe_summary())
        print("All synthetic input rows in original order; these are not confirmed gene hits:")
        for row in result.rows:
            record = row.identifier_record
            print(f"row={record.locator.logical_record} raw={record.raw_identifier} identity={record.status} "
                  f"targets={record.mapping_targets} numeric={row.truth} outcome={row.outcome} "
                  f"qc={row.predicates[0].qc_status} scientific={row.scientific_status} warning={row.warning}")
    require(not Path(temporary).exists())
    print("SYNTHETIC_NUMERIC_DEMO_OK: temporary files removed; no scientific confirmation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
