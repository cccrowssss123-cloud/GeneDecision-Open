"""Local inspection; explicit contract mode prints counts/status only."""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from .identifiers import inspect_identifiers, load_column_contract, load_mapping_reference
from .categories import evaluate_category_files, load_annotation_reference, load_category_condition
from .measurements import inspect_measurements, load_measurement_audit_contract
from .numeric import evaluate_numeric_conditions, load_numeric_condition, load_numeric_context
from .tables import DatasetInspectionError, inspect_table


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's message can contain user paths, values or unknown fields.
        self.exit(2, "CLI_ARGUMENT_ERROR\n")


def _read_declaration(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except (OSError, ValueError):
        raise DatasetInspectionError("DECLARATION_UNAVAILABLE", "a local declaration could not be read") from None
    if len(raw) > limit:
        raise DatasetInspectionError("DECLARATION_BYTE_LIMIT", "a local declaration exceeds the byte limit")
    return raw


def _bundle_command(args, parser) -> int:
    from .bundle import verify_discovery_bundle, write_discovery_bundle
    from .run import run_discovery

    def read_optional(path, loader, limit=1024 * 1024):
        return loader(_read_declaration(path, limit)) if path is not None else None

    try:
        if args.verify_bundle is not None:
            manifest = verify_discovery_bundle(args.verify_bundle)
            status = "BUNDLE_INTEGRITY_VERIFIED"
        else:
            run = run_discovery(
                args.table, load_column_contract(_read_declaration(args.column_contract, 1024 * 1024)),
                mapping_reference=read_optional(args.mapping_reference, load_mapping_reference, 64 * 1024 * 1024),
                annotation_reference=read_optional(args.annotation_reference, load_annotation_reference, 64 * 1024 * 1024),
                category_condition=read_optional(args.category_condition, load_category_condition),
                measurement_audit_contract=read_optional(args.measurement_audit_contract, load_measurement_audit_contract),
                numeric_condition=read_optional(args.numeric_condition, load_numeric_condition),
                numeric_context=read_optional(args.numeric_context, load_numeric_context),
                combine=args.combine, expected_source_sha256=args.expected_source_sha256,
                input_format=args.input_format, encoding=args.encoding, confirm_header=args.confirm_header,
            )
            manifest = write_discovery_bundle(run, args.export_bundle)
            status = "BUNDLE_WRITTEN"
    except DatasetInspectionError as exc:
        parser.exit(2, str(exc) + "\n")
    except (ValidationError, ValueError, TypeError, OSError, RecursionError):
        parser.exit(2, "LOCAL_DISCOVERY_BUNDLE_FAILED\n")

    summary = {
        "status": status, "row_count": manifest.row_count, "candidate_count": manifest.candidate_count,
        "row_truth_counts": manifest.row_truth_counts, "numeric_qc_flagged_rows": manifest.numeric_qc_flagged_rows,
        "warning": manifest.warning, "scientific_validation": manifest.scientific_validation,
        "data_classification": manifest.data_classification,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    else:
        counts = manifest.row_truth_counts
        print(f"{status}; {manifest.warning}; scientific validation: {manifest.scientific_validation}.")
        print(f"{manifest.row_count} rows (T={counts['T']}, F={counts['F']}, U={counts['U']}); "
              f"{manifest.candidate_count} exploratory candidates.")
        if manifest.numeric_qc_flagged_rows is not None:
            print(f"QC_FAILURE_DECLARED_ROWS: {manifest.numeric_qc_flagged_rows}")
    return 0


def main() -> int:
    parser = SafeArgumentParser(prog="genedecision.discovery", description="Inspect one explicitly selected local CSV/TSV.")
    parser.add_argument("table", type=Path, nargs="?")
    parser.add_argument("--format", choices=("CSV", "TSV"), dest="input_format")
    parser.add_argument("--encoding", choices=("utf-8-sig", "utf-8", "utf-16"), default="utf-8-sig")
    parser.add_argument("--confirm-header", action="store_true", help="Confirm intentional numeric header names")
    parser.add_argument("--json", action="store_true", help="Legacy mode: local manifest (possibly sensitive); contract mode: counts/status only")
    parser.add_argument("--column-contract", type=Path, help="Explicit local discovery-column-contract-v1 JSON")
    parser.add_argument("--mapping-reference", type=Path, help="Explicit local reference JSON bound by the column contract")
    parser.add_argument("--annotation-reference", type=Path, help="Explicit local versioned annotation JSON")
    parser.add_argument("--category-condition", type=Path, help="Explicit local ANY/ALL membership condition JSON")
    parser.add_argument("--measurement-audit-contract", type=Path, help="Explicit local lexical measurement audit JSON")
    parser.add_argument("--numeric-condition", type=Path, help="Explicit exploratory numeric condition bound to the measurement audit")
    parser.add_argument("--numeric-context", type=Path, help="Optional bound caller context/QC declarations; never scientific validation")
    local_bundle = parser.add_mutually_exclusive_group()
    local_bundle.add_argument("--export-bundle", type=Path, help="Write exploratory Discovery output to a new local directory")
    local_bundle.add_argument("--verify-bundle", type=Path, help="Verify local Discovery bundle file checksums")
    parser.add_argument("--combine", choices=("ALL", "ANY"), help="Required with both category and numeric conditions in a bundle")
    parser.add_argument("--expected-source-sha256", help="Require bundle export to use previously inspected source bytes")
    args = parser.parse_args()
    if args.verify_bundle is not None:
        if (args.table is not None or args.column_contract is not None or args.mapping_reference is not None
                or args.annotation_reference is not None or args.category_condition is not None
                or args.measurement_audit_contract is not None or args.numeric_condition is not None
                or args.numeric_context is not None or args.combine is not None or args.expected_source_sha256 is not None
                or args.input_format is not None
                or args.confirm_header or args.encoding != "utf-8-sig"):
            parser.error("verify-bundle accepts only a directory and optional JSON output")
        return _bundle_command(args, parser)
    if args.table is None:
        parser.error("a table is required")
    if args.export_bundle is not None:
        if args.column_contract is None:
            parser.error("bundle export requires a column contract")
        return _bundle_command(args, parser)
    if args.combine is not None or args.expected_source_sha256 is not None:
        parser.error("run-specific flags require bundle export")
    if args.mapping_reference is not None and args.column_contract is None:
        parser.error("reference requires contract")
    if args.numeric_condition is not None:
        if args.measurement_audit_contract is None or args.column_contract is None:
            parser.error("numeric conditions require column and measurement audit contracts")
    elif args.numeric_context is not None:
        parser.error("numeric context requires numeric conditions")
    if args.measurement_audit_contract is not None:
        if args.column_contract is None or args.annotation_reference is not None or args.category_condition is not None:
            parser.error("measurement audit requires a column contract and a separate mode")
    if args.annotation_reference is not None or args.category_condition is not None:
        if args.column_contract is None or args.annotation_reference is None or args.category_condition is None:
            parser.error("category evaluation requires all declarations")
    try:
        if args.measurement_audit_contract is not None:
            contract = load_column_contract(_read_declaration(args.column_contract, 1024 * 1024))
            audit = load_measurement_audit_contract(_read_declaration(args.measurement_audit_contract, 1024 * 1024))
            reference = (load_mapping_reference(_read_declaration(args.mapping_reference, 64 * 1024 * 1024))
                         if args.mapping_reference is not None else None)
            condition = (load_numeric_condition(_read_declaration(args.numeric_condition, 1024 * 1024))
                         if args.numeric_condition is not None else None)
            context = (load_numeric_context(_read_declaration(args.numeric_context, 1024 * 1024))
                       if args.numeric_context is not None else None)
            result = inspect_measurements(args.table, contract, audit, mapping_reference=reference,
                                          input_format=args.input_format, encoding=args.encoding,
                                          confirm_header=args.confirm_header)
            if condition is not None:
                numeric = evaluate_numeric_conditions(result, condition, context=context)
                summary = numeric.safe_summary()
                if args.json:
                    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
                else:
                    print("EXPLORATORY_ONLY; SCIENTIFICALLY_UNVERIFIED; scientific validation: NOT_PERFORMED.")
                    print(f"{summary['input_row_count']} data rows.")
                    for outcome, count in summary["outcome_counts"].items():
                        print(f"{outcome}: {count}")
                    print(f"QC_FAILURE_DECLARED_ROWS: {summary['qc_flagged_row_count']}")
                    for status, count in summary["predicate_qc_status_counts"].items():
                        print(f"QC_{status}: {count}")
                return 0
            summary = result.safe_summary()
            if args.json:
                print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
            else:
                print(f"{summary['input_row_count']} data rows, {summary['cell_count']} measurement cells.")
                print("Declarations: USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED; scientific validation: NOT_PERFORMED.")
                for status, count in summary["cell_status_counts"].items():
                    print(f"{status}: {count}")
            return 0
        if args.annotation_reference is not None:
            result = evaluate_category_files(
                args.table, args.column_contract, annotation_reference_path=args.annotation_reference,
                condition_path=args.category_condition, mapping_reference_path=args.mapping_reference,
                input_format=args.input_format, encoding=args.encoding, confirm_header=args.confirm_header,
            )
            summary = result.safe_summary()
            if args.json:
                print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
            else:
                print(f"{summary['input_row_count']} data rows.")
                print("Declarations: USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED; scientific validation: NOT_PERFORMED.")
                for outcome, count in summary["row_outcomes"].items():
                    print(f"{outcome}: {count}")
            return 0
        if args.column_contract is not None:
            contract = load_column_contract(_read_declaration(args.column_contract, 1024 * 1024))
            reference = (load_mapping_reference(_read_declaration(args.mapping_reference, 64 * 1024 * 1024))
                         if args.mapping_reference is not None else None)
            result = inspect_identifiers(args.table, contract, mapping_reference=reference,
                                         input_format=args.input_format, encoding=args.encoding,
                                         confirm_header=args.confirm_header)
            summary = result.safe_summary()
            if args.json:
                print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
            else:
                print(f"{summary['row_count']} data rows, {summary['column_count']} columns.")
                print("Declarations: USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED; scientific validation: NOT_PERFORMED.")
                print("Reference: " + summary["reference_status"])
                print(f"Duplicate rows: raw={summary['raw_duplicate_row_count']}, lexical={summary['lexical_duplicate_row_count']}.")
                for status, count in summary["status_counts"].items():
                    print(f"{status}: {count}")
            return 0
        result = inspect_table(args.table, input_format=args.input_format,
                               encoding=args.encoding, confirm_header=args.confirm_header)
    except DatasetInspectionError as exc:
        parser.exit(2, str(exc) + "\n")
    except (ValidationError, ValueError, TypeError, OSError, RecursionError):
        parser.exit(2, "LOCAL_INSPECTION_FAILED\n")
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True, indent=2))
    else:
        manifest = result.manifest
        print(f"{manifest.input_format}: {manifest.row_count} data rows, {manifest.column_count} columns.")
        print(f"Dataset type: {manifest.dataset_type.value} ({manifest.dataset_type_basis}).")
        print(f"Warnings: {len(manifest.warnings)}; unresolved questions: {len(manifest.unresolved_mapping_questions)}.")
        print("Structural inspection only; scientific validation: NOT_PERFORMED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
