"""Run the existing Discovery APIs on entirely invented software fixtures.

From the repository: PYTHONPATH=src python examples/discovery_categories_demo.py
No files or references are downloaded. Temporary input is removed on exit.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from genedecision.discovery import (
    AnnotationReference, CategoryCondition, ColumnContract, LocalMappingReference,
    MappingEntry, evaluate_categories, inspect_identifiers,
    load_annotation_reference, load_category_condition, load_column_contract,
    load_evaluation_result, load_mapping_reference, replay_evaluation_result,
)


def _require(condition: bool) -> None:
    if not condition:
        raise RuntimeError("SYNTHETIC_DEMO_EXPECTATION_FAILED")


def main() -> int:
    print("SYNTHETIC ONLY: invented identifiers, sources, categories and values.")
    print("Scientific validation: NOT_PERFORMED. No measurement or QC interpretation.")

    source = dict(source_id="SYN_SOURCE", release="SYN_SOURCE_RELEASE_1")
    record_ids = ["SYN_SOURCE_RECORD"]

    def assertion(identifier, state, scope=None):
        return dict(assertion_id=identifier, category_id="SYN_CATEGORY", state=state,
                    source_binding=source, source_record_ids=record_ids,
                    complete_scope_id=scope)

    def entity(identifier, target, assertions):
        return dict(entity_record_id=identifier, target_identifier=target,
                    source_record_ids=record_ids, assertions=assertions)

    # Same-source/release completeness supports explicit negative evidence.
    # Its coverage of missing entities/assertions never creates a negative.
    reference = AnnotationReference.from_declarations(
        reference_id="SYN_ANNOTATION", release="SYN_ANNOTATION_RELEASE_1",
        namespace="synthetic-target", taxon="synthetic-taxon",
        provenance=dict(description="Invented software-demo reference", source_versions=[
            dict(**source, description="Invented source, no biological meaning")]),
        license=dict(identifier="SYN_TEST_ONLY", notes="Invented license metadata",
                     local_use="UNKNOWN", redistribution="UNKNOWN"),
        vocabulary=[dict(category_id="SYN_CATEGORY", label="Invented category",
                         definition="Invented membership set for software demonstration",
                         definition_source="SYN_SOURCE_RECORD")],
        source_records=[dict(record_id="SYN_SOURCE_RECORD", source_binding=source,
                             source_locator="SYN_INVENTED_RECORD_LOCATOR")],
        entities=[
            entity("SYN_ENTITY_T", "SYN_TARGET_T", [assertion("SYN_PRESENT_T", "PRESENT")]),
            entity("SYN_ENTITY_T_ALT", "SYN_TARGET_T_ALT", [assertion("SYN_PRESENT_T_ALT", "PRESENT")]),
            entity("SYN_ENTITY_F", "SYN_TARGET_F", [assertion("SYN_ABSENT_F", "ABSENT", "SYN_COMPLETE_SCOPE")]),
            entity("SYN_ENTITY_NO_ASSERTION", "SYN_TARGET_NO_ASSERTION", []),
            entity("SYN_ENTITY_CONFLICT", "SYN_TARGET_CONFLICT", [
                assertion("SYN_CONFLICT_PRESENT", "PRESENT"),
                assertion("SYN_CONFLICT_ABSENT", "ABSENT", "SYN_COMPLETE_SCOPE"),
            ]),
        ],
        complete_scopes=[dict(
            scope_id="SYN_COMPLETE_SCOPE", source_binding=source,
            covered_identifiers=["SYN_TARGET_T", "SYN_TARGET_T_ALT", "SYN_TARGET_F", "SYN_TARGET_CONFLICT",
                                 "SYN_TARGET_NO_ASSERTION", "SYN_TARGET_NO_ENTITY"],
            category_ids=["SYN_CATEGORY"], coverage_status="COMPLETE",
            scope_definition="Only these invented targets and category",
            source_record_ids=record_ids,
        )],
    )
    mapping = LocalMappingReference.from_entries(
        reference_id="SYN_MAPPING", source_namespace="synthetic-source",
        target_namespace="synthetic-target", taxon="synthetic-taxon",
        entries=tuple(MappingEntry(source=raw, target=target) for raw, target in (
            ("SYN_RAW_TRUE", "SYN_TARGET_T"),
            ("SYN_RAW_TRUE", "SYN_TARGET_T"),  # Duplicate source evidence retained.
            ("SYN_RAW_FALSE", "SYN_TARGET_F"),
            ("SYN_RAW_NO_ENTITY", "SYN_TARGET_NO_ENTITY"),
            ("SYN_RAW_NO_ASSERTION", "SYN_TARGET_NO_ASSERTION"),
            ("SYN_RAW_CONFLICT", "SYN_TARGET_CONFLICT"),
            ("SYN_RAW_AMBIGUOUS", "SYN_TARGET_T"),
            ("SYN_RAW_AMBIGUOUS", "SYN_TARGET_T_ALT"),
        )),
    )
    contract = ColumnContract(
        primary_gene=dict(column="synthetic_feature", definition="Invented source identifier"),
        measurements=[dict(column="synthetic_value", definition="Invented unused measurement")],
        namespace="synthetic-source", taxon="synthetic-taxon",
        mapping_reference=mapping.binding,
    )
    condition = CategoryCondition.from_declarations(
        annotation_binding=reference.binding, association_mode="BOUND_MAPPING_TARGET",
        predicates=[dict(condition_id="SYN_MEMBER_CONDITION", category_id="SYN_CATEGORY")],
        combine="ALL",
    )
    # Use strict public loaders, including canonical digest and duplicate-key
    # protection, before any processing. All reference content remains in memory.
    mapping = load_mapping_reference(mapping.model_dump_json())
    reference = load_annotation_reference(reference.model_dump_json())
    contract = load_column_contract(contract.model_dump_json())
    condition = load_category_condition(condition.model_dump_json())

    raw_ids = ("SYN_RAW_TRUE", "SYN_RAW_FALSE", "SYN_RAW_NO_ENTITY",
               "SYN_RAW_NO_ASSERTION", "SYN_RAW_CONFLICT", "SYN_RAW_AMBIGUOUS",
               "SYN_RAW_TRUE", "SYN_RAW_UNMAPPED")
    with TemporaryDirectory(prefix="genedecision-synthetic-demo-") as temporary:
        table = Path(temporary) / "synthetic.csv"
        table.write_text("synthetic_feature,synthetic_value\n" + "".join(
            f"{raw},{index}\n" for index, raw in enumerate(raw_ids)), encoding="utf-8")
        identifiers = inspect_identifiers(table, contract, mapping_reference=mapping)
        result = evaluate_categories(identifiers, annotation_reference=reference,
                                     conditions=condition, mapping_reference=mapping)
        restored = load_evaluation_result(result.model_dump_json())
        replay_evaluation_result(restored, annotation_reference=reference,
                                 conditions=condition, mapping_reference=mapping)

        _require(result.counts.row_outcomes == dict(INCLUDED=2, EXCLUDED=1, UNKNOWN=5))
        _require(tuple(r.identifier_record.raw_identifier for r in result.rows) == raw_ids)
        _require(tuple(r.truth for r in result.rows) == ("T", "F", "U", "U", "U", "U", "T", "U"))
        _require(result.counts.raw_duplicate_row_count == 2)
        _require(result.rows[0].identifier_record.mapping_entry_count == 2)
        _require(len(result.conflicts) == 1 and len(result.nonmatches) == 1)
        _require(result.rows[5].identifier_record.status == "UNRESOLVED_AMBIGUOUS")
        _require(len(result.rows[5].targets) == 2 and all(t.truth == "T" for t in result.rows[5].targets))
        _require(restored == result)

        print(json.dumps(result.safe_summary(), ensure_ascii=True, sort_keys=True))
        print("Every synthetic input row, in original order (no rank or merge):")
        for row in result.rows:
            identity = row.identifier_record
            targets = ", ".join(f"{t.identifier}:{t.truth}" for t in row.targets) or "NONE"
            print(f"row={identity.locator.logical_record} line={identity.locator.physical_line_end} "
                  f"raw={identity.raw_identifier} identity={identity.status} "
                  f"targets=[{targets}] outcome={row.outcome} reasons={list(row.reasons)}")
        print("Conflicting synthetic assertions retained:")
        for row in result.conflicts:
            for target in row.targets:
                for predicate in target.predicates:
                    for evidence in predicate.evidence:
                        print(f"{evidence.assertion.assertion_id}: {evidence.assertion.state} "
                              f"applicability={evidence.applicability}")
    _require(not Path(temporary).exists())
    print("SYNTHETIC_DEMO_OK: temporary input removed; all rows/evidence retained in memory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
