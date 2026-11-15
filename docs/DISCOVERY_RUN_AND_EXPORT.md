# Run and export local Discovery

Discovery starts with one analyzed CSV or TSV and a column contract. A category
condition, a numerical condition, or both select exploratory rows. Run it
without a starting gene, an account, a provider or a database. The
[synthetic example](../examples/discovery_bundle_demo.py) builds and verifies
two bundles and shows how to combine their candidate sets.

## Run one table

1. Inspect the table with `python -m genedecision.discovery results.csv`.
   Choose the identifier and measurement columns explicitly and create the
   [column contract](DISCOVERY_COLUMN_CONTRACT_V1.md). For resolved candidates,
   provide the exact local mapping reference bound by that contract. A mapping
   records caller-declared identity, with no independent verification.
2. Provide either a [local category reference and condition](DISCOVERY_CATEGORIES_6B3.md),
   or a [measurement audit contract and numerical condition](DISCOVERY_NUMERIC_6B4B.md).
   For the numerical condition, use `inspect_measurements(...)` to obtain the
   bound audit ID, set exact operators and thresholds with
   `NumericCondition.from_declarations(...)`, and save it with
   `model_dump_json()`. Supply the bound caller QC context if the condition
   requires it. A different table or audit contract invalidates the binding.
3. Point the CLI to the declarations and choose a new local directory. Use a
   destination outside the code repository or one you've verified Git ignores.

```sh
python -m genedecision.discovery results.csv \
  --column-contract columns.json \
  --mapping-reference mapping.json \
  --measurement-audit-contract audit.json \
  --numeric-condition numeric.json \
  --export-bundle /path/to/local-results/run-01

python -m genedecision.discovery --verify-bundle /path/to/local-results/run-01
```

For a category run, use `--annotation-reference` and `--category-condition`.
When you specify both categories and numeric conditions, also supply
`--combine ALL` or `--combine ANY`. `ALL` returns F if either side is F,
and T only if both are T. `ANY` returns T if either side is T, and F only
if both are F. The remaining cases are U. Each evaluation keeps its own
truth, evidence and warnings. Use `--expected-source-sha256` with the exact
lowercase SHA-256 from your reviewed input manifest to reject changed bytes,
including for category-only runs.

A row is an exploratory match when its expression is T. A resolved candidate
has at least one matching row and a unique target in the supplied bound
mapping. Candidate keys use the exact declared taxon, target namespace and
identifier. Unmapped or ambiguous matches remain visible as rows with no
candidate key. An exact, directly declared category key can support a category
row match on its own, while a candidate key still requires the unique mapping.
Candidate entries retain their other observed rows as well. The full row
outcomes retain the T, F and U distinction, per-condition provenance and
declared QC failures. Every result carries `SCIENTIFICALLY_UNVERIFIED`.

## Read and check the bundle

| File | Contents |
| --- | --- |
| `run_manifest.json` | Source byte digest, result and bundle identities, counts, software version and artifact checksums |
| `requirements.json` | Column contract, source manifest, reference bindings, condition declarations and supplied numerical context |
| `observations.jsonl` | All parsed rows in source order, unmodified local cell strings, identifiers, measurements and locators |
| `row_outcomes.jsonl` | Per-row truth, category assertions, numerical predicate traces and declared QC failures |
| `candidates.jsonl` | Exact mapped keys, qualifying and companion row locators, and downstream preparation status |
| `candidates.tsv` | A readable row per candidate and qualifying observation, pointing back to its row trace |

Match the two row files on `locator.logical_record`. The locator also records
the physical end line of a record, which can span several lines. TSV taxon,
namespace and identifier fields with a leading spreadsheet formula character
or apostrophe are prefixed with an apostrophe. The JSONL values are exact.
To decode TSV display fields, remove one leading apostrophe when present.

Export reserves a new directory and publishes the manifest after writing all
artifacts. Keep the destination directory private. On POSIX, the exporter asks
for owner-only permissions on the new directory and files. An interrupted run
can leave an incomplete directory with no manifest. A new export always needs
a fresh destination. The verifier checks the fixed file list and each recorded
size and digest. It reports integrity of the supplied files; scientific
validation, source-byte replay and authentication are separate operations.

All exports and terminal summaries can contain unpublished research
information. Keep them outside Git and public services. The runnable examples
generate their own invented inputs and clean up their temporary directories.

## Combine independently evaluated tables

Use `run_discovery(path, column_contract, ...)` once per table with its own
conditions and declarations. Pass those results to
`union_discovery_runs([run_a, run_b])`:

- A key that matches in either run appears in the union. A single valid run is
  sufficient.
- The union lists the matching, nonmatching and unknown rows actually observed
  for that key in each run where it occurs. Use the run ID and row locator to
  retrieve the original trace. A missing entry gives no information about
  testing or coverage in that run.
- Exact repeats of a run are deduplicated. Different filters applied to the same
  source bytes remain separate runs with the same source digest. Source
  independence stays `NOT_ESTABLISHED`.

The union uses the exact declared candidate key. It preserves the per-table
conditions and observations without pooling measurements. Discovery stops at
exploratory selection and provenance. Interpretation preparation is unassessed
and Decision preparation requires a confirmed hypothesis and successful
Interpretations.
