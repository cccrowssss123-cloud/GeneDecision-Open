# Run a complete local Discovery example

To evaluate one table, export a verifiable candidate bundle and combine
independently evaluated tables, use the [Discovery run and export guide](DISCOVERY_RUN_AND_EXPORT.md).

The example uses the existing 6B1/6B2/6B3 public APIs on invented identifiers,
sources, releases, categories and numbers. It creates one temporary CSV, supplies
all references in memory, retains every input row and removes the temporary file
on exit. It downloads nothing and uses no database or scientific provider.

From a repository checkout with its existing Python dependencies available:

```bash
PYTHONPATH=src python examples/discovery_categories_demo.py
```

No repository installation or additional dependency is needed for this example.
Use the project's existing Python interpreter if `python` does not provide its
dependencies. The script has no input-file option: it always constructs its own
synthetic table. It does not read research data or write a persistent result.

## What it does

1. Creates a versioned `AnnotationReference` with a defined category, fixed source
   release, provenance records, license declarations and explicit coverage scope.
2. Creates a local identifier mapping with a duplicate entry and a one-to-many
   key. Creates a `ColumnContract` selecting exact headers and binding that mapping.
3. Creates an explicit `CategoryCondition` using BOUND_MAPPING_TARGET and ALL.
   The strict public JSON loaders verify these declarations and their digests.
4. Calls `inspect_identifiers` once on the temporary table. The result includes
   the 6B1 manifest and locators from the same byte snapshot. Measurement values
   are present in the table but are not interpreted or used in category conditions.
5. Calls `evaluate_categories` with the required mapping and annotation references,
   then loads the result structurally and separately replays the supplied references.
6. Checks the fixture's expected partitions, identities, duplicates, conflict
   evidence, ambiguous targets and serialization round trip. It prints counts and
   the complete synthetic row trace, then checks temporary-directory cleanup.

## Expected behavior

The eight rows yield **2 INCLUDED, 1 EXCLUDED, 5 UNKNOWN**. The two included rows
are separate occurrences of the same raw input; neither is collapsed. There are
two rows affected by raw duplication, one conflict row and one ambiguous row.

| Synthetic case | Result and reason |
| --- | --- |
| PRESENT target, repeated source row | T / INCLUDED; both rows and duplicate mapping-entry evidence are retained |
| Explicit ABSENT plus matching COMPLETE scope | F / EXCLUDED; source, release, target and category all match |
| Missing entity or missing category assertion | U / UNKNOWN, even when the target is in a COMPLETE scope |
| Applicable PRESENT and ABSENT assertions | U / CONFLICT; both assertions remain visible |
| Two mapped targets, both individually T | Row U / UNKNOWN; STRICT_UNKNOWN preserves unresolved identity despite agreement |
| No mapping hit | U / UNKNOWN; there is no raw-key fallback |

The output begins with `SYNTHETIC ONLY` and `Scientific validation: NOT_PERFORMED`.
A successful run ends with `SYNTHETIC_DEMO_OK`. Trace order follows input row
locators; it is not a ranking. An unexpected outcome raises a fixed demo failure
instead of claiming success. This is an executable integration example, not
scientific validation or evidence that a real reference is correct.

The result retains `confirmed_matches`, `nonmatches`, `unknowns` and `conflicts`
views. These views do not delete original rows. Conflict diagnostics can overlap
determinate whole-expression outcomes in multi-condition ANY/ALL queries; the
single-category conflict shown here is UNKNOWN. Scope completeness alone never
makes an omitted assertion FALSE.

## Use the public interfaces with local declarations

The same API sequence accepts explicitly supplied local column/mapping/annotation
and condition declarations. Each reference must match its binding exactly; do not
replace reference truth with targets copied from an unverified serialized result.
For a read-once file-based entry, use `evaluate_category_files` as described in
[the category contract documentation](DISCOVERY_CATEGORIES_6B3.md). The CLI requires
explicit `--column-contract`, `--annotation-reference`, `--category-condition`
and, for mapped mode, `--mapping-reference`; it displays counts and fixed statuses.

The direct association alternative is an explicit DECLARED_DIRECT_KEY with
RAW_EXACT, matching namespace/taxon, and no mapping binding or supplied mapping.
It preserves original identity status and does not use the 6B2 lexical result to
trim, change case, remove versions or infer aliases. This example uses the mapped
path so that ambiguity and reference replay are visible.

Loading result JSON checks internal structure and digest consistency. Reference
replay verifies supplied frozen declarations; neither operation confirms current
table bytes or scientific validity. To verify current source bytes, run the table
entry again and compare its input/processing identities. Digests are content
identities, not signatures or scientific provenance certification.

## Research confidentiality and next-stage limits

Only this hardcoded, invented demo trace is suitable for reviewed source/example
delivery. Real raw IDs, measurements, definitions, source metadata, manifests,
result traces **and aggregate counts/statuses** can all be unpublished research
information. Keep them within authorized local/OpenAI processing and outside the
code repository or in verified excluded local storage. Do not copy real content
into this example, PRs, CI logs, search queries or third-party systems. Reduced
`safe_summary` output is not permission to transmit research results. License
declarations and a private repository do not waive the policy.

6B1/6B2 measurement roles and numeric-like profiling remain declarations and
lexical diagnostics. Measurement semantics, tested universe, QC, scientific
missingness and any numerical candidate conditions are next-stage design topics;
this example implements none of them. It introduces no default p/FDR/effect
thresholds, scoring, priority, causal interpretation, Decision or multi-dataset
join. Existing category truth and identity rules remain unchanged.

6B4A separately adds same-snapshot raw measurement and lexical auditing through
`inspect_measurements` and the opt-in `--measurement-audit-contract` CLI mode.
It preserves caller text, exact values and all original rows without numerical
conditions or scientific assessment. See [the measurement audit guide](DISCOVERY_MEASUREMENTS_6B4A.md)
for its independent contract, counts, engineering limits and fresh-source verification.
The category example above continues to use its original 6B1/6B2/6B3 APIs.

Exploratory numeric comparisons remain independently visible with
insufficient QC/tested-universe documentation. 6B4B supplies a separate bound
condition API and visibly unverified output; it does not change category truth or
the measurement audit contract. See [the numeric workflow](DISCOVERY_NUMERIC_6B4B.md)
and `examples/discovery_numeric_demo.py` for a complete invented example, including
declared failed QC, unresolved identity, reference replay and fresh-source checking.
