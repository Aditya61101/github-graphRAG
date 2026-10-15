# ADR applicability enrichment

New ADR ingestion still performs one combined, structured extraction call per primary
chunk at temperature zero. Applicability is added to decisions and constraints in that
same response. Existing decision/constraint IDs, relationship extraction, entity
extraction, and atomic ADR replacement are retained. This implementation adds no PR
evaluation, code-derived relationships, SQLite migration, or vector index.

## Implementation map

| File | Responsibility |
| --- | --- |
| `applicability.py` | Strict reusable Pydantic `Applicability`, `ApplicabilityTarget`, `ApplicabilityCondition`, and persisted `ApplicabilityMetadata`; versions and JSON validation |
| `models.py` | Shared applicability on decision/constraint extraction and graph records |
| `context.py`, `extractor.py` | Bounded document navigation/context, primary-source-only extraction, applicability prompt, supplied evidence validation |
| `chunker.py` | Preserve section levels alongside unchanged chunk IDs/text |
| `resolver.py`, `scope_resolver.py` | Read-only reference matching using canonical entity identities and actual repository community metadata/membership |
| `neo4j_writer.py` | Applicability JSON and flat retrieval hints inside existing ADR transaction |
| `applicability_store.py` | Repository-scoped reads, candidate hint ranking, guarded applicability-only writes |
| `enrichment.py` | Chunk-grouped in-place enrichment, progress, errors, resume, and association refresh |
| `preview.py` | Validated preview artifacts, original-source fingerprints, and legacy report compatibility |
| `../../runs/run_adr_applicability.py` | Reviewable command; defaults to read-only planning |
| `../../../api_services/app/main.py` | Configure new-ingestion document-context budget |
| `../../tests/test_adr_applicability.py`, `../../tests/test_adr_neo4j_pipeline.py` | Focused enrichment tests and existing graph regression coverage |

## Semantic contract and storage

`scopes` can contain multiple values: `repository`, `community`, `entity`, `other`,
and `unknown`. Conditions are independent: language, directory, file category,
operation, layer, workflow, or other. Natural-language conditions, exceptions,
qualifiers, scope basis (`explicit`, `inferred`, `uncertain`), rationale, semantic
targets, and evidence chunk references are retained. Literal selectors are optional;
the prompt forbids inventing paths, languages, selectors, graph IDs, or restrictions.

The LLM schema does not accept resolved entity/community IDs. Code assigns versions
and resolves identifiers. Persisted JSON is a validated `ApplicabilityMetadata`
envelope with schema/prompt/resolution versions, semantic applicability, resolved IDs,
unresolved target names, graph snapshot hash, and context truncation flag.

Flat properties on both knowledge labels include `applicability_scopes`,
`applicability_repository_wide`, `applicability_entity_ids`,
`applicability_community_ids`, `applicability_languages`, and
`applicability_unresolved`. Enrichment status, model/revision label, version fields,
and `applicability_enriched_at` are separate from original timestamps. Existing
`DEFINES`, `SUPPORTS`, `AFFECTS`, `CONSTRAINS`, GraphAssertions, and embeddings retain
their existing meanings. There are no new entity-to-entity or `GOVERNS` edges.

`ADRApplicabilityStore.list_candidates(repository_id, ...)` ranks entity, community,
repository, language, and fallback hints. It never excludes a record using scope
metadata. Paginate to inspect all repository knowledge. Legacy or invalid metadata
remains readable as unknown applicability, with raw properties still available.

## Document context and graph associations

The current primary chunk remains the only source of new requirements. The prompt
includes the ADR title, section hierarchy, and other chunks prioritized by generic
scope/condition/exception language, constructed without a summarization call.
Default additional context is 6,000 characters, configurable with
`ADR_DOCUMENT_CONTEXT_MAX_CHARS`. Primary text has its existing chunk budget.
Only chunk IDs actually supplied can be cited. Truncation is explicit and retained
as uncertainty. Evidence from another section is stored in applicability metadata;
the original requirement's `SUPPORTS` edges are not widened.

Entity scope matching reuses deterministic ADR matching rules (label/name, aliases,
compatible case-insensitive name, whitespace/hyphen/underscore normalization).
Absent/ambiguous targets remain unresolved; scope targets alone never create entities.

A community association requires a community target with evidence, a supported scope
basis, and `governs_entire_scope=true`. Its semantic name must match an actual
repository community name, title, architectural role, or alias, allowing spacing/casing
normalization. Actual `MEMBER_OF` membership must exist. Multiple matching communities
are supported. A requirement merely affecting an entity never inherits its communities.
Summaries are loaded for context/graph snapshot information, but are not fuzzy-matched
to invent subsystem identities. Missing associations do not fail extraction.

`--refresh-associations` re-resolves the stored semantic targets against current graph
membership and metadata, with no LLM call. It updates applicability JSON, association
IDs, unresolved hint, and `applicability_resolved_at`; original extraction versions,
model, timestamps, provenance, and semantics stay unchanged. Run after community
membership changes as needed; this task does not alter or hook community clustering.

## Backfill command — review before running

No live Neo4j backfill was executed during implementation.

The command reads existing persisted decisions/constraints, supporting chunks, and
other chunks belonging to their ADR. It never uses normal upload and never re-extracts
the requirement graph. Requirements with the same ADR/supporting chunks are batched
(default 12). The model must return every supplied requirement ID exactly once and
cannot return rewritten descriptions or new requirements in its strict schema.

Required connection variables in `backend/.env`:

- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`; optional `NEO4J_DATABASE` (default `neo4j`).
- For preview or fresh extraction with plain `--apply`: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`,
  `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_DEPLOYMENT_NAME`.
- No embedding configuration is required. Saved-preview import, plain planning, and association refresh need
  no Azure connection. Configure the deployment name even for planning if you want
  completed-version skip counts to match extraction.

Use the canonical repository ID stored on ADR nodes, not `owner/name`. To identify it
in Neo4j Browser, run this read-only query:

```cypher
MATCH (adr:ADR)-[:DEFINES]->(n)
WHERE n:ArchitecturalDecision OR n:ArchitecturalConstraint
RETURN adr.repository AS repository_id, count(DISTINCT adr) AS adrs,
       count(DISTINCT n) AS requirements
ORDER BY repository_id;
```

From PowerShell:

```powershell
Set-Location "E:\Studies\Dev\Gen AI\github-graphrag\backend"
$adrRepositoryId = "<canonical repository ID>"

# Read-only plan: no model calls and no database writes.
.\.venv\Scripts\python.exe -m ai_services.runs.run_adr_applicability --repository-id $adrRepositoryId --dry-run --report adr-applicability-plan.json

# Preview: model calls, validated outputs in the report, no database writes.
.\.venv\Scripts\python.exe -m ai_services.runs.run_adr_applicability --repository-id $adrRepositoryId --preview --report adr-applicability-preview.json

# Validate the saved preview against current Neo4j data: zero LLM calls and no writes.
.\.venv\Scripts\python.exe -m ai_services.runs.run_adr_applicability --repository-id $adrRepositoryId --from-preview adr-applicability-preview.json --dry-run

# Save exactly the reviewed preview results: zero LLM calls.
.\.venv\Scripts\python.exe -m ai_services.runs.run_adr_applicability --repository-id $adrRepositoryId --from-preview adr-applicability-preview.json --apply --report adr-applicability-applied.json

# Refresh community/entity associations later, without extracting again.
.\.venv\Scripts\python.exe -m ai_services.runs.run_adr_applicability --repository-id $adrRepositoryId --refresh-associations --dry-run --report adr-applicability-refresh-plan.json
.\.venv\Scripts\python.exe -m ai_services.runs.run_adr_applicability --repository-id $adrRepositoryId --refresh-associations --apply
```

`--from-preview` never constructs an Azure client or calls the model. It validates the
whole report before writing: repository/requirement IDs, completeness, metadata schema,
ADR evidence ownership, original-source fingerprints when available, and current graph
associations. Reviewed semantic metadata and resolved IDs are saved exactly; changed
associations are rejected rather than silently replaced. Writes use the existing
applicability-only transactions. Rerunning the same import skips identical applied
results and resumes unfinished batches. The input preview file is preserved; the command
rejects an output `--report` path that would overwrite it. Plain `--apply` without
`--from-preview` still performs fresh extraction and incurs LLM calls.

New previews record repository/model versions and snapshots of requirement text, source
hashes, provenance/navigation/chunk content, supporting evidence, and previous metadata.
This allows detecting source changes since preview generation without another model call.

Existing older reports (including the previously generated 171-result preview) are
supported. They did not record source fingerprints or the original model revision, so
import validates their IDs, evidence, and associations against the current database but
cannot detect earlier source-text changes. This limitation is reported in `warnings`.
The model is recorded as `legacy-preview-unknown` unless an explicit `--model-version`
identifies the deployment/revision that generated that report. No model provenance is
inferred from current Azure settings. Missing/conflicting IDs, foreign evidence,
different existing enrichment, and changed associations are rejected before any writes.

Rerun the same apply command after interruption: completed records matching schema,
prompt, and model version are skipped. Each batch commits atomically and validates
original requirement kind/title/description, ADR hash, evidence IDs, previous metadata,
and chunk text/hash before commit. A concurrently changed requirement aborts its batch
and is retried from fresh data on the next run. Failures are reported independently and
persist an applicability error/status when a guarded update is still possible. Original
knowledge timestamps, IDs, descriptions, source fields, hashes, evidence, and entity
relationships are never replaced by backfill.

Use `--force` to deliberately regenerate current metadata. Use a consistent
`--model-version deployment-or-revision` across runs when the deployment's actual model
revision changes without its name changing. `--batch-size`, `--context-max-chars`, and
`--input-max-chars` tune bounds (defaults: 12, 6000, 24000). Oversized requests are
reported rather than silently dropping requirement text; lower the batch size or
increase the input budget. Reports include planned/updated/skipped/failed counts,
model-call counts, and error details; exit code is nonzero for failed batches.

## Representative outputs

These are expected structured fixtures used in offline tests, not outputs from a live
model or the existing 171-node database.

| Requirement | Scope | Conditions / exceptions | Graph association |
| --- | --- | --- | --- |
| All application variables follow camelCase | repository, explicit | Applicable variable identifiers; no invented language restriction | None required |
| Public REST endpoints authenticate, except health checks | other (broad API scope), explicit | Handling public REST requests; health-check exception | Unresolved semantic scope is retained |
| Document-processing subsystem communicates asynchronously | community, explicit | Named semantic subsystem | Only evidence-backed matches to actual metadata and membership |
| Verify policy ownership before claim creation | other (workflow), explicit | Before claim creation | No automatic repository scope |
| Python files in `src/jobs/` use the job interface | repository + other, explicit | Literal language and directory selectors | Independent of conditions |
| Prefer bounded retries when appropriate | unknown, uncertain | Ambiguous operations retained | Fallback discovery |

## Validation and limits

Run the focused suite from `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest ai_services/tests/test_adr_preview_import.py ai_services/tests/test_adr_applicability.py ai_services/tests/test_adr_neo4j_pipeline.py ai_services/tests/test_adr_upload.py -q
```

Tests cover fixtures A–D, entity/community/overlapping scopes, conditions and exceptions,
cross-section evidence, truncated context, injection-bearing text and system protections,
strict SDK schemas, JSON round trips, repository isolation, legacy records, unchanged
graph identities/provenance, existing extraction and atomic replacement, guarded writes,
backfill idempotency, interruption recovery, and association refresh.

Validation is offline with mocked LLM/Neo4j behavior. It verifies application contracts
and query guards, not live model accuracy or server-side Cypher execution. Review preview
outputs against source ADRs before applying to the existing database. Existing communities
without matching semantic names/roles/aliases stay unresolved. Context prioritization is
lexical and bounded; implicit, distant, or non-English scope may remain uncertain.
Stage 4.1 should treat all associations as retrieval signals, include unknown/legacy
fallbacks, evaluate natural-language conditions/exceptions against PR context, and refresh
associations after graph changes. No PR retrieval or evaluation is implemented here.

Implementation validation: 96 focused ADR tests passed, including saved-preview import,
unchanged reviewed results, legacy compatibility, source conflicts, zero Azure calls,
provenance preservation, and interrupted-import recovery. The earlier extended ADR/canonicalization/
shared-knowledge/repository-graph/knowledge-explorer run passed 102 tests and exposed one
existing failure in `test_invalid_entity_names_are_rejected_and_writer_preserves_reserved_identity_fields`:
its RKG fixture omits the embedding required by the unchanged RKG writer. This failure
also reproduces in isolation; RKG code/tests were left unchanged. Ruff lint and formatting
passed for the 13 checked ADR implementation/tooling/new-test files, and `git diff --check`
passed. Lint findings in untouched ADR service/parser files were not part of this change.
