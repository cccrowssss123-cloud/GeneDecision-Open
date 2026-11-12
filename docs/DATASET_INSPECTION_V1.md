# Local dataset inspection — Milestone 6B1

GeneDecision's Discovery foundation inspects one local, already-analyzed CSV or
TSV result table without a preselected gene, hypothesis, omics platform or
database. It returns structural metadata, tentative column-role suggestions,
unresolved mapping questions and a readable summary. Candidate generation,
scientific thresholds, annotation and downstream evaluation are future work.

## Programmatic use

```python
from genedecision.discovery import inspect_table

inspection = inspect_table("results.tsv")  # an explicitly selected local file
manifest = inspection.manifest
print(inspection.summary)
print(manifest.model_dump_json(indent=2))
for question in inspection.unresolved_mapping_questions:
    print(question.code, question.columns, question.question)
```

The caller may provide structural column declarations and a known dataset type:

```python
from genedecision.discovery import ColumnMapping, ColumnRole, DatasetType

inspection = inspect_table(
    "results.csv",
    mappings=[
        ColumnMapping(column="feature", role=ColumnRole.GENE_IDENTIFIER),
        ColumnMapping(
            column="metric", role=ColumnRole.MEASUREMENT,
            definition="Definition supplied by the analysis author",
        ),
    ],
    dataset_type=DatasetType.CRISPR,
    missing_tokens_by_column={"metric": ["NA", "N/A"]},
)
```

These declarations are retained as `USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED`.
Even a supplied statistical definition remains a scientific validation question.
No statistic is converted into significance, hypothesis support or priority.
Explicit mappings must name actual, unique header columns and are stored in
header order. Identifier namespaces, species, aliases and categories are not
resolved in this milestone.

## Local CLI

The package follows the existing `python -m` convention. From a repository
checkout, without an install step:

```bash
PYTHONPATH=src .venv/bin/python -m genedecision.discovery results.csv
PYTHONPATH=src .venv/bin/python -m genedecision.discovery results.tsv --json
```

Default CLI output contains counts and fixed statuses only. The programmatic
`inspection.summary` retains its 6B1 behavior, including local header metadata.
`--json` prints a `TableInspection` object containing `manifest`, `summary` and
`row_locators`. Questions are in `manifest.unresolved_mapping_questions`.
Structural failure exits with code 2 and a sanitized error code/physical line.
Neither output contains the selected path, basename or raw cell values.
Output stays on stdout; the operation creates no persistent artifact directory.

Supported options: `--format CSV|TSV`, `--encoding utf-8-sig|utf-8|utf-16`,
`--confirm-header`, `--json`. The legacy mode exposes structural inspection. Milestone 6B2 adds an explicit
`--column-contract` mode; see `DISCOVERY_COLUMN_CONTRACT_V1.md`. Legacy missing-token
policies remain programmatic.
No credentials, NCBI email, Codex runtime, database or web server are required.

## Manifest contract

`DatasetManifest` is a Pydantic model with `extra="forbid"`, frozen fields and
schema version **`dataset-manifest-v1`**. Its JSON Schema is available locally:

```python
from genedecision.discovery import DatasetManifest
schema = DatasetManifest.model_json_schema()
```

| Field group | Meaning |
| --- | --- |
| Identity | `dataset_id`, `inspection_id`, `source_file_sha256`, `source_file_size`, `source_scope=USER` |
| Parser | CSV/TSV `input_format`, delimiter, explicitly chosen encoding, caller header confirmation, missing-token comparison policy |
| Structure | Data row count, column count, ordered original header names, separately counted blank records |
| Columns | Tentative header-based role, blank count, declared nonblank missing count, possible missing-marker count, lexical numeric-like count or `null` for identifiers, declared missing tokens |
| Mapping | Ordered caller mappings, supplied definitions and explicit unvalidated semantics; unresolved questions |
| Dataset type | UNKNOWN by default; CRISPR/RNA_SEQ/SCRNA_SEQ/OTHER only by caller declaration, with its basis |
| Validation | Deterministically ordered warnings and mapping questions |
| Time | Timezone-aware `inspected_at`; caller can fix it for a reproducible export |

`dataset_id` is `DATASET_` followed by the source byte SHA-256. Renaming the same
bytes preserves it. It identifies a source table, not a biological study or an
independent experiment. Future scientific dataset identities must also bind
analysis context rather than treating equal/different files as study identities.

`inspection_id` is `INSPECTION_` followed by SHA-256 of canonical manifest
content, excluding `inspection_id` and `inspected_at`. It binds parser settings,
counts, suggestions, mappings, dataset-type declaration, missing-token policies
and diagnostics. `manifest.identity_payload()` exposes that reproducible content.
Changing these settings changes the inspection identity even if input bytes stay
the same. Timestamp alone does not. Dataset/inspection identities and header/count
relationships are revalidated on loading a serialized manifest.

No raw rows, example identifiers, experimental design, treatment/control,
species, sample size, statistical cutoff, category memberships or biological
conclusions are included. Header names and caller-supplied definitions are part
of the metadata, so callers should review them before sharing a manifest. The
manifest is local metadata, not a public upload or permission to transmit data.

`TableInspection.row_locators` contains only logical data-record indices and
physical ending line numbers; multiline quoted records remain locatable without
exporting their content. The manifest itself contains no row payloads.

## Deterministic validation and preservation

CSV uses commas and TSV uses tabs. The suffix selects the format unless the
caller explicitly supplies `input_format`. There is no delimiter sniffing. A
mislabeled file may parse as one column; `SINGLE_COLUMN` asks the caller to check
format rather than silently trying another delimiter. Explicit format selection
also allows other suffixes such as a user-declared TSV `.txt`.

Default encoding is `utf-8-sig`, which handles UTF-8 with/without BOM. UTF-8 and
BOM-marked UTF-16 can be selected explicitly. Decode errors, decoded NUL or an
unhandled BOM fail; no replacement characters or encoding guessing are used.

Empty files, header-only tables, unnamed headers, duplicate names after whitespace
stripping, wrong row widths and malformed quoting fail without repair. Raw header
spelling/order is preserved; case-distinct column names remain distinct. Blank
records are counted separately and warned about, matching the existing reader's
data-row behavior. A row of explicitly delimited empty cells is a data record.

A first record containing numeric header tokens requires explicit header
confirmation. This guards common headerless numeric tables while allowing
intentional numeric labels with `confirm_header=True`. A headerless table whose
first row contains plausible text labels is inherently ambiguous; confirmation
remains an unresolved question until supplied. Inspection is not proof that an
upstream file had a header.

Errors expose codes and optional physical line numbers without source paths or
offending cells. Byte/row limits default to 64 MiB/1,000,000 records and are
configurable through the API. They are operational limits, not scientific
thresholds. The source is opened read-only. The exact bytes are read once and
hashed; a private, automatically removed temporary snapshot lets the unchanged
`read_evidence_rows` utility parse those same bytes. Concurrent source edits
cannot change what the returned manifest describes. Inspection does not promise
that the source will remain unchanged by other programs after it returns.

## Suggestions, identifiers and missingness

Exact case-insensitive header hints recognize names such as `gene`,
`gene_symbol`, `symbol`, `gene_id`, `ensembl_gene_id`, `entrez_gene_id`,
`p_value`, `pvalue`, `fdr`, `padj`, `q_value`, `effect` and `log2FoldChange`.
These are suggestions only. Several gene-like columns produce an ambiguity
warning; none is selected. Unrecognized names need explicit mapping. `score`
has UNKNOWN meaning even when every value looks numeric. Column order and name
matching do not establish an annotation, experimental design or statistic.

Raw cells remain strings inside the existing reader, retaining leading zeros,
long numeric IDs, date-like text, scientific-notation-like IDs and spaces. Gene
hints or explicit identifier mappings suppress numeric profiling entirely.
Other columns may have a finite numeric-token count; this describes lexical
parseability only. No values are exported as coerced numbers, no dates are
parsed, and no p-value domains/adjustment semantics are certified here.
Non-numeric tokens in a putative measurement column trigger a warning; they are
neither repaired nor discarded.

Blank/whitespace cells are counted as blank. Nonblank missingness requires an
explicit per-column token policy, recorded with stripped, case-insensitive
matching. Common NA-like spellings in other columns are only *possible* markers
until confirmed. They are not treated as missing in identifier columns unless
explicitly declared. Zero remains a present numeric token. The original source
spelling is unchanged. Structural missing cells do not mean NOT_MEASURED or a
negative scientific observation; signal-state classification is deferred.

## Boundaries relative to the 6A proposal

6A proposed the wider CRISPR Discovery MVP. This implementation follows the 6B1
request's narrower structural contract and broader platform independence:

- It does not require CRISPR, gene-level analysis, taxon or DiscoveryContext to
  inspect a file. Dataset type defaults to UNKNOWN. Those facts become required
  only for future operations that depend on them.
- The structural schema uses `dataset-manifest-v1`; the proposed future
  `candidate-discovery-v1` would describe a full candidate workflow.
- A small module CLI is included now because 6B1 requests a usable local entry
  point; the wider run/export CLI remains deferred.
- The legacy raw reader rejects bad header/width/empty input but uses permissive
  CSV quoting and does not accept an in-memory stream. A small strict validation
  pass plus temporary byte snapshot adds the needed guarantees without changing
  the reader or scientific normalization contracts.

No EvidenceRecord, Interpretation, Decision, storage or connector source changes
are required. The top-level package already imports its existing library modules;
importing them creates no database/service run. New inspection code invokes none
of them beyond the raw reader and common error base.

## Verification and next dependency

`tests/test_discovery_tables.py` uses synthetic tables only and guards transport,
database, Interpretation and Decision execution. It covers quoting,
malformed/empty inputs, identifier strings, mappings, missingness, encoding,
stable hashes, source preservation, local export and CLI use.

Historical 6B1 records reported **46 focused tests passed** and **1012 passing
regression tests**. These are historical results, not a new verification in 6B2.
The current 6B2 run uses only synthetic tests in the safe code copy, without
historical artifacts, benchmark inputs or real research databases. It makes no
claim of a fresh preservation audit of those materials. Historical tests remain
unchanged; only the authorized development documentation is updated here.

6B2 adds explicit column, namespace/taxon and optional pinned local-reference
contracts. Statistical validation, tested-universe/QC context, scientific
missingness, filtering and annotations require separate contracts before future
candidate discovery can run.

## Milestone 6B2 compatibility

`inspect_table` defaults, its return type, `dataset-manifest-v1`, raw-row reader,
header suggestions and historical structural checks remain compatible. A private
helper now returns the inspection and raw rows from the same byte snapshot to
6B2; the public 6B1 API still returns metadata only. Identifier processing does
not reopen the source after inspection.

The new explicit `discovery-column-contract-v1` and local
`discovery-identifiers-v1` result are described in
[DISCOVERY_COLUMN_CONTRACT_V1.md](DISCOVERY_COLUMN_CONTRACT_V1.md). CLI default
output now omits headers and digests. Legacy `--json` remains an explicitly
requested local metadata export and can contain sensitive headers/definitions.
All outputs, including counts/status from real inputs, remain subject to
AGENTS.md and DATA_ACCESS_POLICY.md; reduced output is not permission to upload
research results. Only synthetic examples/test evidence may accompany code.
