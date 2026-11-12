# Local category membership (Milestone 6B3)

Discovery can evaluate one dataset against one caller-supplied local annotation
reference and an explicit ANY/ALL membership condition. Categories, definitions,
sources, releases, licenses and completeness are declarations by the caller.
The software verifies their structure and binding; it does not certify their
scientific validity. Existing `inspect_table`, `dataset-manifest-v1` and 6B2
identifier interfaces retain their behavior.

This milestone contains no provider retrieval, category hierarchy inference,
ranking, score, measurement threshold, resistance or causal interpretation,
Decision, QC exclusion, multi-dataset join or consensus option. A single dataset
suffices. Future union with optional evidence remains a design extension.

## Contracts and provenance

`AnnotationReference` has schema `discovery-annotation-reference-v1` and semantics
`category-assertion-v1`. Its content digest covers all fields except the digest
itself, including array order, provenance, source releases, vocabulary definitions,
source-record locators, license declarations, entities, assertions and scopes.
Sources have exact `(source_id, release)` bindings. Every source record must bind
a declared source release. Definitions cite a source-record ID. Entities preserve
separate source rows for the same target. Assertion IDs are globally unique;
repeated same-direction assertions use separate IDs and remain separate evidence.

`CompleteScope` declares an exact enumerated target set, category set, source
release, evidence records, definition and COMPLETE/PARTIAL/UNKNOWN coverage.
There is no implicit global universe. Within a source release, overlapping target
and category sets cannot declare different coverage statuses. Validation compares
scope sets without materializing their Cartesian product. Evaluation caches
membership sets once and preserves original scope order and digest content.

`CategoryCondition` has schema `discovery-category-condition-v1` and semantics
`category-membership-three-valued-v1`. It binds the full annotation identity,
including reference ID, release, digest, namespace and taxon. It requires a
nonempty ordered list of uniquely named MEMBER_OF predicates, explicit ANY/ALL,
and an explicit association mode. Unknown categories and unsupported fields or
policies are rejected. Policies are fixed to STRICT_UNKNOWN and RETAIN_SEPARATELY.

Both contracts are frozen Pydantic declarations with forbidden extra fields and
strict strings/counts. `from_declarations(**fields)` validates declarations and
computes their canonical digest. Loaders verify existing digests. Public JSON
loaders reject duplicate keys at every nesting level, nonstandard constants and
invalid UTF-8. `model_json_schema()` provides the contract schemas. Use the public
loaders for untrusted serialized inputs; generic JSON parsing can discard duplicate
keys before validation. Public processing errors contain fixed codes and messages.

## Association and membership

DECLARED_DIRECT_KEY requires `direct_key_policy="RAW_EXACT"`, an annotation with
the column contract's exact namespace/taxon, no mapping binding and no supplied
mapping reference. It looks up the unchanged raw identifier. The 6B2 lexical
policy and lexical result are retained but do not transform the direct key.
Blank or declared-missing identifiers remain unresolved. This association is a
caller declaration; `identity_verification` remains NOT_VERIFIED and the original
6B2 identity status remains unchanged, including UNRESOLVED_NO_REFERENCE.

BOUND_MAPPING_TARGET requires the column contract's mapping binding and its
matching local mapping reference. Annotation namespace must match the mapping's
target namespace; taxon must match exactly. The evaluator replays 6B2 lexical
lookups and checks every target and mapping-entry count, including duplicate
entries. Missing required references are configuration errors. A valid reference
with no matching row produces UNKNOWN. There is no fallback to the raw key.

PRESENT supports T without requiring a completeness scope. ABSENT supports F
only when its explicit scope is COMPLETE and matches the assertion's source,
release, target and category. An omitted assertion, missing entity or target
outside coverage never supports F, even with a COMPLETE scope elsewhere.
Missing source records or nonexistent nonempty scope references are structural
errors. Existing but inapplicable scopes preserve their assertions and fixed
reasons: SOURCE_SCOPE_MISMATCH, SOURCE_RELEASE_SCOPE_MISMATCH or
OUTSIDE_ASSERTION_SCOPE. Such evidence does not compete as a determinate negative.

An ABSENT assertion without a scope, or with matching PARTIAL/UNKNOWN coverage,
is UNDETERMINED. Applicable UNCERTAIN assertions are also UNDETERMINED. Either
keeps the predicate U even alongside a determinate assertion. Scope-inapplicable
evidence is retained separately; it alone yields U and does not overturn an
applicable determinate assertion. Applicable positive and negative assertions
produce U/CONFLICTING across sources as well as within one source. Evidence is
never voted on, overwritten or selected by source priority.

| Predicate truths | ANY | ALL |
| --- | --- | --- |
| T, T | T | T |
| T, F | T | F |
| T, U | T | U |
| F, F | F | F |
| F, U | U | F |
| U, U | U | U |

Each target evaluates the entire condition before row aggregation. Every 6B2
one-to-many ambiguous row stays UNKNOWN, even if all targets have the same
condition result. All targets remain visible. Different targets cannot jointly
satisfy an ALL expression. Single-target rows use that target's expression result.
Targets with a determinate expression retain decisive condition IDs alongside
every other predicate and its evidence.

Predicate conflicts are U. An independent T can still determine ANY, or an
independent F can determine ALL, without erasing the conflicting predicate.
The conflict view therefore includes every row containing a predicate conflict,
including rows whose complete expression is determinate. Conflict and reason
counts are overlapping diagnostics, never an additional outcome partition.

## Local API and verification boundaries

Public imports are available from `genedecision.discovery`:

```python
load_annotation_reference(json_bytes)
load_category_condition(json_bytes)
evaluate_categories(identifiers, annotation_reference=reference,
                    conditions=condition, mapping_reference=mapping)
inspect_and_evaluate_categories(table_path, column_contract,
                               annotation_reference=reference,
                               conditions=condition, mapping_reference=mapping)
evaluate_category_files(table_path, column_contract_path,
                        annotation_reference_path=annotation_path,
                        condition_path=condition_path,
                        mapping_reference_path=mapping_path)
load_evaluation_result(json_bytes)
replay_evaluation_result(result, annotation_reference=reference,
                         conditions=condition, mapping_reference=mapping)
```

The table wrapper reuses 6B1/6B2's bounded byte snapshot and reader. File-based
declarations are each read once into bounded bytes: 1 MiB for column contracts
and conditions, 64 MiB for mapping and annotation references. There are no
implicit file reads or external source requests. Table parser options, byte/row
limits and inspection timestamp retain their existing meaning. Changes to a
source after its snapshot is captured do not alter that evaluation.

`EvaluationResult` uses `discovery-category-evaluation-v1`. It retains the full
6B2 inspection, column contract, annotation and condition contracts, explicit
input binding, all original row/locator/duplicate records and all target/predicate
evidence. Mapping reference identity is bound through the column contract;
mapping entries must be supplied separately for replay. The evaluation identity
binds these contracts, byte identity, processing identity, semantics, validation
level, rows and counts. File read paths and operational timestamps are excluded;
declared reference provenance locators are included.

Table/file wrappers record FRESH_SNAPSHOT_PROCESSING. `evaluate_categories`
records SERIALIZED_INPUT_REFERENCE_REPLAY: it validates the supplied 6B2 object
and replays any required mapping, but does not reread a source table.
`load_evaluation_result` validates internal structure, digests, evidence retention,
bindings, locators, counts and expression consistency. Its retained validation
level describes the original operation, not a new source/reference verification
by the loader. Recorded evidence applicability is not externally authenticated
by loading the result. `replay_evaluation_result` requires separately supplied,
digest-matched annotation, condition and any required mapping to recompute the
result. It does not reread the table. To check current table bytes, run the table
wrapper again and compare `input_binding.processing_id` and byte identity.
Digests are content identities, not signatures or scientific provenance proofs.

The primary denominator is `input_row_count=N` and
`INCLUDED + EXCLUDED + UNKNOWN = N`. Blank CSV records follow existing 6B1
semantics and are counted separately. Associated-key occurrences count repeated
targets or direct keys per row; predicate occurrences count keys × conditions.
Identity, duplicate, ambiguity and conflict row counts use input rows; reason
counts can overlap. These counts do not represent distinct biological units.
`confirmed_matches`, `nonmatches`, `unknowns` and `conflicts` are accessible views
over retained rows. Zero confirmed matches is valid for a nonempty input.

## CLI and confidentiality

Add `--annotation-reference LOCAL_JSON --category-condition LOCAL_JSON` to
existing column-contract mode. Both require `--column-contract`; mapped mode
also requires `--mapping-reference`. Default category output and `--json` output
contain only counts and fixed statuses. Errors do not echo supplied paths,
headers, values, definitions, provenance or digests. The CLI does not export
the detailed result. Legacy modes retain their existing behavior, including
legacy `--json` manifest output, which may contain sensitive information.

All detailed API outputs are LOCAL_RESEARCH_DATA_DO_NOT_UPLOAD. Real annotation
definitions, identifiers, source metadata and aggregate counts can also be
unpublished research information. Keep real inputs and derived outputs outside
the code repository or in verified excluded local storage. Local/OpenAI processing
permission and license declarations do not authorize GitHub, search, external
logging or other third-party disclosure. Repository fixtures and validation in
this milestone use invented software-test inputs only. Review the full diff and synthetic test evidence before publishing changes.
