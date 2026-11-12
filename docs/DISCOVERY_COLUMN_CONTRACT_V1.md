# Explicit column contracts and local identifier auditing — Milestone 6B2

This foundation requires the caller to select exactly one primary identifier
column and at least one measurement column. Every selected column needs a
nonblank definition. Actual header spelling must match exactly; duplicate
headers, duplicate selections, incompatible roles and extra fields fail. A
`score` header has no established statistical meaning. Definitions, namespace,
taxon and any local mapping reference are user declarations, recorded as
`USER_DECLARED_NOT_SCIENTIFICALLY_VALIDATED`; none is scientifically certified.

All examples below are invented synthetic software fixtures. The namespace,
taxon, identifiers and measurement definition assert no biological facts.

## Explicit JSON contract and local API

```json
{
  "schema_version": "discovery-column-contract-v1",
  "primary_gene": {
    "column": "feature",
    "role": "GENE_IDENTIFIER",
    "definition": "Synthetic source identifier"
  },
  "measurements": [
    {
      "column": "score",
      "role": "MEASUREMENT",
      "definition": "Synthetic arbitrary measurement"
    }
  ],
  "namespace": "synthetic-source",
  "taxon": "synthetic-taxon"
}
```

The synthetic table has the following literal bytes:

```csv
feature,score
SYNTHETIC_001,0.25
```

```python
from pathlib import Path
from genedecision.discovery import load_column_contract, inspect_identifiers

# Caller-created synthetic files only; no source or reference is discovered.
contract = load_column_contract(
    Path("/private/tmp/synthetic-discovery/contract.json").read_bytes()
)
result = inspect_identifiers(
    "/private/tmp/synthetic-discovery/table.csv", contract
)
print(result.safe_summary())
# result.identifiers contains original identifier strings and row locators.
# result.model_dump_json() is a LOCAL research-data export, never a GitHub artifact.
```

`ColumnContract`, `GeneColumn`, `MeasurementColumn`, `LexicalPolicy`,
`MappingBinding`, `LocalMappingReference`, `MappingEntry`, `IdentifierRecord`
and `IdentifierInspection` are exported from `genedecision.discovery`. Versioned
models are frozen, forbid extra fields and provide local JSON Schema through
`model_json_schema()`. Text fields require strings; booleans and counts use
strict types. JSON arrays serialize ordered tuples. `load_column_contract`,
`load_mapping_reference` and `inspect_identifiers` sanitize declaration errors
into fixed diagnostic codes, suppressing Pydantic input details. Both public
JSON loaders reject duplicate object keys recursively, before any value can be
overwritten, including nested column definitions and reference entries. They
also reject nonstandard JSON numeric constants; declaration bytes use UTF-8. Direct low-level
Pydantic validation is for local use; callers must avoid logging its structured
errors, which may carry sensitive field names or inputs.

Namespace and taxon are mandatory, exact, nonblank caller declarations. They
are not inferred or checked against a biological catalogue. Measurement roles
are `MEASUREMENT`, `P_VALUE`, `ADJUSTED_P_VALUE` or `EFFECT`; assigning a role does
not verify its definition, method, scale, adjustment or valid range. Unselected
columns remain part of the 6B1 inspection and are not used as identifiers.

## Preservation, missingness and duplicates

Default `lexical_policy` is `{"strip_whitespace": false, "case": "PRESERVE"}`.
The original string always remains in `raw_identifier`. Leading zeros, long
numeric strings, date-like strings, case and identifier version suffixes survive.
No aliases, versions, gene categories or namespaces are resolved automatically.
Optional caller-requested whitespace stripping or `UPPER`/`LOWER` case operations
produce a separate `lexical_identifier` and are retained in the contract. Case
operations use Python Unicode string casing; they are lexical operations and
provide no scientific validity. No version-removal operation is implemented.

Empty and whitespace-only cells are `BLANK`, retaining their exact original
string and locator. `missing_identifier_tokens` is an optional list of exact
raw-string spellings: no trimming or case folding is used for this list. Tokens
such as `NA` are present identifiers unless explicitly declared missing. Declared
missingness is tested before lexical transformations. Blank status takes
precedence for whitespace-only cells. Measurements keep 6B1 profiling/missingness
behavior; this contract adds no measurement missingness or scientific QC policy.

Every data record produces one identifier record; no rows are dropped, averaged
or merged. Each record carries a consecutive logical data-record index and the
physical ending line from the existing reader, including multiline quoted
records. Completely blank CSV/TSV records remain separately counted by 6B1.
`raw_occurrence_count` counts identical original strings, including repeated
missing strings. `lexical_occurrence_count` counts identical present policy-derived
strings; missing records have zero. This reveals collisions introduced by an
explicit lexical policy. Summary duplicate counts are the number of affected
rows, not the number of groups or the number beyond the first occurrence.

## Optional caller-supplied local reference

No reference file is opened implicitly, and no annotation library or provider is
included. A caller can supply an in-memory reference or explicitly name local
reference JSON to the CLI. A reference has version `local-identifier-reference-v1`,
a caller-declared `reference_id`, source/target namespace, taxon, ordered entries
and a verified SHA-256. The digest covers canonical JSON of all reference fields
except `content_sha256`, retaining entry order and duplicates. It is a content
digest of the serialized reference, not a hash of the reference file's whitespace.

```python
from genedecision.discovery import (
    ColumnContract, LocalMappingReference, MappingEntry, inspect_identifiers,
)

reference = LocalMappingReference.from_entries(
    reference_id="synthetic-reference-v1",
    source_namespace="synthetic-source",
    target_namespace="synthetic-target",
    taxon="synthetic-taxon",
    entries=(MappingEntry(source="SYNTHETIC_001", target="SYNTHETIC_TARGET_001"),),
)
# Bind an existing explicit contract to this exact caller-supplied reference.
payload = contract.model_dump(mode="json")
payload["mapping_reference"] = reference.binding.model_dump(mode="json")
bound_contract = ColumnContract.model_validate(payload)
result = inspect_identifiers(
    "/private/tmp/synthetic-discovery/table.csv", bound_contract,
    mapping_reference=reference,
)
```

The contract binding must exactly match reference identity, digest, namespaces
and taxon. Source namespace and taxon must also match the contract declarations.
No cross-taxon mapping or namespace inference occurs. Reference keys match the
policy-derived identifier exactly; keys are not automatically normalized.

| Status | Meaning |
| --- | --- |
| `BLANK` / `DECLARED_MISSING` | Original cell retained; no mapping lookup |
| `EMPTY_AFTER_POLICY` | Reserved for a lexical operation yielding empty text; current whitespace-only inputs are classified as `BLANK` first |
| `UNRESOLVED_NO_REFERENCE` | No reference bound in the contract |
| `UNRESOLVED_REFERENCE_NOT_SUPPLIED` | Contract binds a reference but caller did not supply it |
| `UNRESOLVED_NOT_FOUND` | No exact key in supplied reference |
| `UNRESOLVED_AMBIGUOUS` | Multiple distinct targets retained; none selected |
| `RESOLVED_BY_LOCAL_REFERENCE` | One distinct target in the caller-supplied reference; scientific validity remains unverified |

Repeated identical mapping pairs remain counted in `mapping_entry_count`, even
if they yield one distinct target. Ambiguity preserves all distinct targets in
`mapping_targets`; the caller is never forced to accept one. These target strings
are local research data. No reference status establishes a reference truth.

## Input and contract binding

The same bounded, read-once input bytes supply the strict inspection, SHA-256,
legacy raw-string reader and identifier records. The private byte snapshot is
deleted after parsing. A later modification of the original file cannot replace
the strings processed while retaining the old hash. The operation does not
promise the source stays unchanged after it returns.

`IdentifierInspection` includes its `TableInspection`, complete column contract,
canonical `contract_sha256`, whether a reference was supplied, and all identifier
records. Its `processing_id` binds the inspection identity, contract digest,
reference-supplied flag and complete identifier records. Timestamp alone does not
change this identity. JSON loading revalidates contract digest, selections,
locators, lexical/missingness policy, duplicate counts, status consistency and
processing identity. These digests detect content changes; they do not certify
an upstream dataset, reference or scientific claim. Revalidating lookup targets
against reference entries requires the explicitly supplied reference and a fresh
local processing call.

## CLI and disclosure boundary

```bash
python -m genedecision.discovery /private/tmp/synthetic-discovery/table.csv \
  --column-contract /private/tmp/synthetic-discovery/contract.json
python -m genedecision.discovery /private/tmp/synthetic-discovery/table.csv \
  --column-contract /private/tmp/synthetic-discovery/bound-contract.json \
  --mapping-reference /private/tmp/synthetic-discovery/reference.json --json
```

In contract mode both text and JSON output contain only aggregate counts and
fixed statuses. Neither prints paths, headers, declarations, original identifiers,
mapping targets, reference identities or content hashes. The CLI creates no
persistent exports. Local API result serialization retains original identifiers
and is explicitly classified `LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD`. Contract JSON,
reference JSON and 6B1 manifests can contain sensitive metadata as well.

`safe_summary` means fewer displayed fields; it does **not** mean permission to
share. Counts and statuses derived from real inputs can themselves be unpublished
analysis results. Keep all real-data output, including summaries and CLI logs,
within the authorized local/OpenAI scope in AGENTS.md and DATA_ACCESS_POLICY.md.
Do not upload it to GitHub, searches, third-party APIs, pages or external logs
without separate destination-specific authorization. Only obviously synthetic
examples and synthetic test results may accompany the source code.

The CLI uses fixed diagnostics for argument/contract/reference failures, rather
than echoing argparse values or Pydantic errors. A failure exits with code 2.
Contract input is limited to 1 MiB and reference input to 64 MiB; tables retain
6B1's configurable API byte/row bounds. These are operational limits, not QC.
Legacy no-contract mode defaults to counts/status; explicitly requested legacy
`--json` retains the 6B1 local metadata export. `inspect_table` itself and
`dataset-manifest-v1` remain unchanged in public behavior.

## Scope and verification

The implementation performs no ranking, candidate filtering, statistical
thresholds, tested-universe inference, biological interpretation, Decision,
SQLite execution, network resolution or automatic 6B3 work. Method, tested
universe, QC and biological validity remain unknown unless separately declared
and evaluated in future authorized work.

`tests/test_discovery_identifiers.py` uses invented references and tables. It
covers strict selections/definitions, string and empty-cell preservation,
explicit lexical collisions, local-reference ambiguity and digest checks,
changing-source snapshot binding, local serialization, sanitized errors and CLI
output. It shares the 6B1 dependency guard against transport, database,
Interpretation and Decision calls. Focused regression includes the existing
6B1 discovery, ingestion, evidence and project tests; historical artifacts and
real benchmark inputs are outside this development test run.
