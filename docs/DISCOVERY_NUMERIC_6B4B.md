# Exploratory numeric conditions (6B4B)

Exploratory numeric comparisons report T/F/U separately from scientific validation.
Missing QC or tested-universe documentation does not erase a computable comparison.
All outputs remain SCIENTIFICALLY_UNVERIFIED, scientific validation NOT_PERFORMED,
scientific status NOT_VALIDATED and scientific hit status NOT_CONFIRMED. A caller
QC PASS does not confer scientific validity; a declared FAIL remains explicitly
visible without changing the numerical observation. No scientific eligibility,
significance, resistance direction, ranking or Decision is inferred.

## Runnable synthetic workflow

From the checkout, using an interpreter with the project's existing dependencies:

```bash
PYTHONPATH=src python examples/discovery_numeric_demo.py
```

This complete public-API example creates one temporary invented table, explicit
column and measurement-audit contracts, a fixed local mapping, a measurement
audit, scoped QC declarations, then an audit-bound numeric condition. It uses the
strict JSON loaders, evaluates, loads the result, replays independent frozen
inputs/references, and verifies a fresh source snapshot. It removes temporary
files on exit. It does not accept a real input path, install/download anything,
call a database or provider, or perform scientific QC.

The five rows produce 3 T, 1 F, 1 U. One T has missing QC, one T has declared FAIL
and one T has declared PASS; all remain unverified. F and U also carry visible
declared QC failures. Two duplicate raw IDs each retain two mapping targets and
UNRESOLVED_AMBIGUOUS identity. Their row comparisons remain visible, but they are
**not confirmed matches for either target gene**. Trace order is input order.
The fixture threshold is invented, explicit software input, not a scientific default.

## Contracts

`NumericCondition` uses `discovery-numeric-condition-v1`, semantics
EXPLORATORY_NUMERIC_ISSUE7_V1. It binds the exact 6B4A `measurement_audit_id` and,
when supplied, a `NumericContext` digest. It requires explicit ANY or ALL and an
ordered nonempty list of at most 128 uniquely named `NumericPredicate` declarations.
Each names an already selected measurement ID and caller provenance. A defined
predicate requires LT/LE/GT/GE/EQ plus an explicit string threshold. There is no
default numerical operator, threshold, p/FDR domain, unit conversion, log/absolute
transformation, measurement meaning or biological direction.

Thresholds reuse the fixed 6B4A ASCII finite-decimal grammar and resource limits,
without whitespace stripping or missing markers. Booleans, floats, nonfinite
values, locale forms and over-limit constants are rejected with fixed public
errors. A legitimately unresolved predicate declares a nonblank `unresolved_reason`
and leaves the operator or threshold undefined; it produces U. Unsupported or
inconsistent structure is an error, not an inferred comparison. Invalid data
cells instead remain U with their original 6B4A parse states and raw strings.

Finite comparisons reconstruct exact Decimal tuples and perform only comparisons;
no rounding, arithmetic or float conversion occurs. This remains independent of
the current Decimal precision, rounding and arithmetic traps. A declared p-value
or effect role remains a caller declaration, even when its numbers can be compared.
T means this declared comparison passed; F means it failed, never biological
absence, lack of signal, QC pass or evidence against a gene.

ANY(T,U)=T and ALL(F,U)=F; U is never converted to F. All individual predicates,
their cells and decisive predicate IDs remain visible. Comparisons are per input
row, not per inferred biological entity. Blank/unmapped/ambiguous IDs do not alter
an independently computable numerical T/F and are not resolved by it. No rows are
merged, averaged or created for genes absent from the table. Category conditions
and category truth remain wholly separate; mixed category/numeric expressions,
ranking, multi-dataset joins and scientific verification are not implemented.

## QC and contextual declarations

`NumericContext` (`discovery-numeric-context-v1`) binds the same measurement audit,
a context ID/release, source records, optional raw context/tested-universe text
and zero or more `QCDeclaration` objects. Text is retained verbatim, never mined
for QC, tested membership, scientific missingness or contradictions. No universe
is inferred from table rows or omissions. A missing context is legal when the
condition explicitly binds no context. A mismatching declared digest is a
configuration error, distinct from insufficient scientific metadata.

Each QC declaration has an ID, UNKNOWN/DECLARED_PASS/DECLARED_FAIL, source/release
binding, existing source-record IDs, explicit selected measurement IDs and either
DATASET scope or exact ROWS locators. Source records provide source ID/release,
locator and statement. References must exist and match the declared release;
row locators must match this snapshot exactly. RAW ID duplicates do not propagate
row-specific QC. Dataset scope applies only to its listed measurements in this
bound audit. No lookup, external certification or automatic QC calculation occurs.

Each numerical predicate retains all applicable QC declaration IDs and source
records through the frozen context. No applicable declaration yields UNKNOWN;
one distinct declared status yields that status. Multiple distinct statuses are
MIXED_DECLARATIONS, preserving competing/unknown statements without source voting,
priority or a scientific contradiction claim. Any applicable DECLARED_FAIL also
sets a separate failure flag, including in mixed declarations. Nothing changes
the T/F/U comparison. A row with T and any applicable failure among its referenced
measurements is EXPLORATORY_NUMERIC_MATCH_QC_FLAGGED. Other T rows are
EXPLORATORY_NUMERIC_MATCH, F rows EXPLORATORY_NUMERIC_NONMATCH, and U rows
NUMERIC_UNKNOWN. Every row carries the same scientific-unverified warning and
NOT_CONFIRMED status. Failures on nondecisive referenced predicates remain visible.

## APIs, binding and verification

Public imports from `genedecision.discovery`:

```text
load_numeric_condition / load_numeric_context / load_numeric_evaluation
evaluate_numeric_conditions(measurement_audit, condition, context=None, max_evaluations=...)
replay_numeric_evaluation(result, measurement_audit=..., condition=..., context=..., mapping_reference=...)
verify_numeric_source(table_path, result, mapping_reference=..., max_evaluations=..., **source_limits)
```

The condition/context constructors `from_declarations` validate and compute a
canonical SHA-256; loaders verify existing digests. Frozen strict models reject
extra fields, duplicate JSON keys and nonstandard constants. Public boundaries
deeply rebuild nested model/Mapping inputs, including copied legacy row locators.
There are no caller-controlled scientific-confirmed status values.

`NumericEvaluationResult` (`discovery-numeric-evaluation-v1`) retains the complete
measurement audit, condition, context, source/identity/mapping bindings, original
row records and cells, predicates, QC applicability, outcomes and counts. Its ID
binds all declarations, raw values, exact representations, evidence, policies and
results. Operation timestamps and read paths are excluded; declared source
locator text and array order are included. There is no automatic rebinding of a
condition prepared for different source bytes or declarations.

Loading performs **internal** audit/parse/comparison/applicability/count/digest
replay. Rehashing an incorrect comparison cannot bypass this replay, but a
self-consistent replacement of both frozen audit and its numerical results still
does not prove source provenance. `replay_numeric_evaluation` requires independently
supplied matching audit/condition/context and the corresponding mapping reference
when it was supplied for the original audit. It replays targets/counts against
that reference; no source table is read. An originally omitted mapping reference
must remain omitted, preserving the original unresolved state.

`verify_numeric_source` uses the frozen 6B4A contracts/parser settings and reference
supply to capture current source bytes once and compare the full audit/evaluation
identity. Fresh source verification does not certify scientific metadata, caller
QC or biological meaning. Digests are content identities, not signatures. The
historical audit creation label is not a new verification performed by loading.

## CLI, limits and confidentiality

Prepare a condition binding the audit of the exact selected source and contracts:

```bash
python -m genedecision.discovery LOCAL_TABLE.csv \
  --column-contract LOCAL_COLUMNS.json \
  --measurement-audit-contract LOCAL_AUDIT.json \
  --numeric-condition LOCAL_NUMERIC_CONDITION.json \
  --numeric-context LOCAL_CONTEXT.json --json
```

Context is optional when unbound; a mapping reference can be supplied using the
existing flag. Category flags cannot be combined with numeric mode. All selected
files are explicitly read once into bounded inputs: 1 MiB per column/audit/numeric
condition/context declaration and 64 MiB for a mapping reference. The table still
uses the existing single snapshot. No references are automatically downloaded.

Default text and JSON output show fixed status labels and counts only, with the
unverified warning. Text also prints failure-flagged row counts and predicate QC
status counts, making QC failures visible on F/U as well as T rows. No identifiers,
headers, thresholds, paths, definitions, provenance or digests are printed.

Input rows are the primary denominator: row T+F+U=N. Outcome counts partition the
same N. Predicate occurrences are N times the explicit predicate count; predicate
truth/QC/reason counts use that denominator. QC failure counts are additional,
overlapping diagnostics, not a new row partition. Raw duplicate/ambiguous counts
are affected rows, not unique genes or independent biological replicates. Zero
exploratory matches is valid. `exploratory_matches`, `qc_flagged_matches`,
`nonmatches` and `unknowns` are views over all retained rows.

`max_evaluations` defaults to 1,000,000 row/predicate occurrences; it must be a
strict positive integer and is checked before constructing numerical result rows.
This is an operational limit, not a scientific cutoff. Existing byte/row/cell
limits remain available during fresh-source verification. Neither over-limit
processing nor a missing file silently returns a partially filtered dataset.

All detailed objects/exports are LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD. Real raw data,
candidate observations, metadata and even counts/status summaries remain within
authorized local/OpenAI processing. Do not put them in GitHub, Issues, PRs, CI,
searches or third-party logs. Only reviewed source and invented examples/tests
belong in the code delivery. The demo's synthetic trace is explicitly labeled;
do not replace it with real research data.
