# Measurement string and lexical audit (6B4A)

`inspect_measurements` records every selected measurement alongside the existing
identifier audit from one byte snapshot. It preserves raw strings, row locators,
duplicate occurrences and all mapping targets. It records caller context/source
text verbatim and reports lexical parse states. It performs no numerical filter,
QC evaluation, tested-universe membership, scientific missingness inference,
probability-domain validation, direction interpretation, eligibility or ranking.
Existing 6B1/6B2/6B3 schemas, identity semantics and category outcomes stay intact.

## Explicit audit contract and API

The contract is `discovery-measurement-audit-contract-v1`, independent of the
existing column contract. Every selected measurement column must have exactly
one uniquely named policy; no unselected column is allowed. Policy order is the
output cell order. The contract binds the exact column-contract digest, policies,
fixed parser semantics/limits, optional caller context and source declarations.
Its canonical digest includes array order and excludes only itself.

This complete example uses invented software-test values and definitions:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from genedecision.discovery import (
    ColumnContract, MeasurementAuditContract, inspect_measurements,
    load_measurement_audit_result, verify_measurement_source,
)

column = ColumnContract(
    primary_gene={"column": "synthetic_feature", "definition": "Invented ID"},
    measurements=[{"column": "synthetic_value", "definition": "Invented value"}],
    namespace="synthetic-source", taxon="synthetic-taxon",
)
audit = MeasurementAuditContract.from_declarations(
    column_contract_sha256=column.content_sha256,
    policies=[{
        "measurement_id": "SYN_MEASURE",
        "column": "synthetic_value",
        "strip_ascii_whitespace": True,
        "missing_tokens": ["SYN_MISSING"],
    }],
    caller_context_text="Invented metadata; no scientific certification",
    source_declarations=[{
        "record_id": "SYN_RECORD",
        "source_locator": "SYN_SOURCE_LOCATION",
        "text": "Invented source declaration, retained verbatim",
    }],
)
with TemporaryDirectory() as temporary:
    path = Path(temporary) / "synthetic.csv"
    path.write_text(
        "synthetic_feature,synthetic_value\n"
        "SYN_A,0007.2500\nSYN_A,SYN_MISSING\nSYN_B,NA\n",
        encoding="utf-8",
    )
    result = inspect_measurements(path, column, audit)
    print(result.safe_summary())  # This example is synthetic only.
    loaded = load_measurement_audit_result(result.model_dump_json())
    fresh = verify_measurement_source(path, loaded)
    assert fresh.audit_id == result.audit_id
```

The three cells are FINITE_DECIMAL, DECLARED_MISSING_TOKEN and NON_NUMERIC.
Two identical identifiers remain two rows. Optional mapping references follow
the existing 6B2 rules: bindings must match, all targets remain visible and an
omitted bound reference remains UNRESOLVED_REFERENCE_NOT_SUPPLIED. Measurement
capture does not resolve ambiguity or change this identity status.

## Lexical rules

The fixed `ASCII_FINITE_DECIMAL_V1` grammar is:

```text
[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?
```

Policies default to preserving whitespace; explicit `strip_ascii_whitespace=True`
strips only ASCII space, tab, CR, LF, VT and FF for parsing. Original strings always
remain unchanged. RAW_EXACT nonblank missing tokens match the original string
before stripping, with no case folding or automatic NA-like markers. An empty or
all-ASCII-whitespace raw string is BLANK. Non-ASCII whitespace is not silently
stripped or classified as a blank. Zero is a value unless its exact spelling is
explicitly declared as a missing token.

| State | Meaning |
| --- | --- |
| BLANK | Empty/all-ASCII-whitespace raw string; no inferred scientific missing state |
| DECLARED_MISSING_TOKEN | Exact caller-declared raw token; no NOT_MEASURED or negative inference |
| FINITE_DECIMAL | Matches the grammar and engineering bounds; no scientific domain check |
| NONFINITE_TOKEN | Signed/case-insensitive Inf/Infinity/NaN/sNaN spellings, including numeric NaN payloads |
| NON_NUMERIC | Other syntax, including undeclared NA, locale separators, percent, fractions, dates, underscores or Unicode digits |
| PARSER_LIMIT | Exceeds an engineering token/digit/exponent bound; original value remains present |

Finite values use `Decimal` construction and `as_tuple()`, with no float,
normalization, arithmetic or active-context rounding. `ExactDecimal` stores sign,
digits and exponent: `-0.00` remains sign 1, digits `(0,)`, exponent -2. Alternate
text spellings can have equal numeric values without losing original strings.
These representations do not infer units, methods, p/FDR validity or effects.
Even a column declared P_VALUE can contain a finite negative value in this audit;
its lexical state is not certification that the declared role is scientifically valid.

Fixed parser bounds are 4096 raw-token characters, 1024 significant coefficient
digits after leading zeros, and exponent magnitude 10000. Both the written
exponent and exact Decimal representation exponent must fit. Limits are embedded
in the contract, cannot be changed within this parser version, and never repair,
round, drop or classify values as scientific FALSE. PARSER_LIMIT is checked before
blank/token parsing when the original token is too long.

## Same snapshot, identities and verification

The new entry uses the existing 6B1 snapshot/reader once. A shared private pure
builder produces exactly the existing 6B2 identifier records and processing
identity; measurement cells come from those same frozen rows. The source is not
reopened after identification. Original profiling and missing-token statistics
retain their 6B1 meaning and may differ from the explicitly declared audit policy.

`MeasurementAuditResult` uses `discovery-measurement-audit-v1`. It retains the full
IdentifierInspection, input binding, audit contract/digest, original rows and
ordered cells, counts and `audit_id`. The identity binds all raw cell strings,
parse inputs/states/exact values, declarations and count/locator relationships.
Operational timestamps and the read path are excluded; caller-declared source
locator text is included. Declared source text such as NOT_MEASURED or QC FAIL
is stored verbatim, never interpreted as a contradiction or eligibility result.

Public models/Mapping inputs are deeply revalidated, including legacy locators in
each output row. The strict JSON loaders reject duplicate keys recursively,
nonstandard constants, extra fields and forged digest relationships. Loading
`MeasurementAuditResult` replays lexical parsing from stored raw strings and
validates row/cell/count relationships. It **does not** prove those stored strings
came from the bound source bytes or verify serialized mapping targets against a
reference. The historical FRESH_SNAPSHOT_RECORDING label describes the creation
operation, not a new source check performed by loading.

`verify_measurement_source(path, result, mapping_reference=...)` captures a new
snapshot with the frozen contracts and original parser settings, then compares
the complete audit identity. It returns a freshly processed matching audit or a
fixed mismatch error. A previously supplied mapping reference must be supplied
again; a previously omitted reference must remain omitted. A supplied reference
must match its binding, and fresh lookups verify its targets/counts. A self-consistent
rehashed serialized cell can pass internal loading but fail this fresh-source check.
Digests establish content identities, not scientific provenance or signatures.

API table byte/row bounds retain their existing defaults. `max_cells` is a strict
positive integer, default **1,000,000**, for both inspection and fresh verification.
The new layer checks `input_rows × selected_measurements` before constructing any
cell models. Over-limit audits fail with MEASUREMENT_CELL_LIMIT, without partial
output, row deletion or scientific FALSE. This operational ceiling is independent
of audit identity, like existing byte/row ceilings; callers can raise it explicitly
for a permitted local workload. Fixed parser limits have a separate meaning.

## Counts, CLI and confidentiality

`input_row_count` counts all data rows; blank CSV records remain separate under
6B1. `measurement_column_count` counts selected policies and `cell_count=N×M`.
The six cell-status counts partition cells. Identity statuses partition rows;
duplicate/ambiguous row counts are additional diagnostics, not biological sample
counts. No category/numerical outcome partition is created by this audit.

CLI opt-in mode requires both explicit contracts:

```bash
python -m genedecision.discovery LOCAL_TABLE.csv \
  --column-contract LOCAL_COLUMN_CONTRACT.json \
  --measurement-audit-contract LOCAL_MEASUREMENT_AUDIT.json --json
```

Mapped inputs can also supply `--mapping-reference`. Category flags and measurement
audit flags cannot be combined; conflicting modes fail with a fixed argument error.
Legacy inspection, identifier and category modes retain their prior behavior.
Declarations are bounded at 1 MiB (column/audit) and 64 MiB (mapping reference).
Default and JSON audit output contain only fixed statuses and counts, with no
paths, headers, raw cells, context/source text, identifiers or digests. Errors are
sanitized, including Pydantic/argument failures.

Detailed API output is LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD. Real raw values, source
metadata, manifests and even CLI/status/count aggregates may be unpublished
research results. Keep them within authorized local/OpenAI processing and outside
the code repository or in verified excluded local storage. No GitHub/CI/search/
third-party disclosure is authorized by a reduced summary or private repository.
All development fixtures here are invented. This stage does not implement the
6B4B measurement-specific scientific applicability or numerical-condition protocol.
