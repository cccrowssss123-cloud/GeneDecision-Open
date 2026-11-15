"""Build and verify two local bundles from invented tables, then take their union."""

from pathlib import Path
from tempfile import TemporaryDirectory

from genedecision.discovery import (
    ColumnContract, LocalMappingReference, MappingEntry, MeasurementAuditContract,
    NumericCondition, inspect_measurements, run_discovery, union_discovery_runs,
    verify_discovery_bundle, write_discovery_bundle,
)


def main() -> int:
    mapping = LocalMappingReference.from_entries(
        reference_id="SYN_MAPPING", source_namespace="synthetic-raw",
        target_namespace="synthetic-key", taxon="synthetic-taxon",
        entries=tuple(MappingEntry(source=f"SYN_RAW_{suffix}", target=f"SYN_KEY_{suffix}")
                      for suffix in ("A", "B", "BOTH")),
    )
    columns = ColumnContract(
        primary_gene={"column": "feature", "definition": "Invented source identifier"},
        measurements=[{"column": "value", "definition": "Invented numeric value"}],
        namespace=mapping.source_namespace, taxon=mapping.taxon, mapping_reference=mapping.binding,
    )
    audit_contract = MeasurementAuditContract.from_declarations(
        column_contract_sha256=columns.content_sha256,
        policies=[{"measurement_id": "SYN_MEASUREMENT", "column": "value"}],
    )
    with TemporaryDirectory(prefix="genedecision-synthetic-bundle-") as directory:
        root = Path(directory)
        runs = []
        for name, text in (
            ("a", "feature,value,note\nSYN_RAW_A,8,invented\nSYN_RAW_BOTH,9,first\nSYN_RAW_BOTH,1,repeat\n"),
            ("b", "feature,value,note\nSYN_RAW_B,7,invented\nSYN_RAW_BOTH,6,second\n"),
        ):
            source = root / f"{name}.csv"
            source.write_text(text, encoding="utf-8")
            audit = inspect_measurements(source, columns, audit_contract, mapping_reference=mapping)
            condition = NumericCondition.from_declarations(
                measurement_audit_id=audit.audit_id, combine="ALL",
                predicates=[{"predicate_id": "SYN_COMPARISON", "measurement_id": "SYN_MEASUREMENT",
                             "operator": "GE", "threshold": "5", "provenance": "Invented demo comparison"}],
            )
            run = run_discovery(
                source, columns, mapping_reference=mapping, measurement_audit_contract=audit_contract,
                numeric_condition=condition, expected_source_sha256=audit.identifiers.inspection.manifest.source_file_sha256,
            )
            destination = root / f"bundle-{name}"
            manifest = write_discovery_bundle(run, destination)
            if verify_discovery_bundle(destination) != manifest or manifest.candidate_count != 2:
                raise RuntimeError("SYNTHETIC_BUNDLE_VERIFICATION_FAILED")
            runs.append(run)

        union = union_discovery_runs(runs)
        by_key = {candidate.key.identifier: candidate for candidate in union.candidates}
        observed_runs = {key: len(candidate.evidence) for key, candidate in by_key.items()}
        if observed_runs != {"SYN_KEY_A": 1, "SYN_KEY_B": 1, "SYN_KEY_BOTH": 2}:
            raise RuntimeError("SYNTHETIC_UNION_EXPECTATION_FAILED")
        if sum(len(entry.nonmatching_rows) for entry in by_key["SYN_KEY_BOTH"].evidence) != 1:
            raise RuntimeError("SYNTHETIC_UNION_TRACE_FAILED")
        print("SYNTHETIC ONLY; SCIENTIFICALLY_UNVERIFIED")
        print("Two verified local bundles; exploratory candidates by number of observed runs:", observed_runs)
    print("SYNTHETIC_BUNDLE_DEMO_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
