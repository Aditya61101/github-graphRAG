# Phase 4.1 architectural context

This package builds immutable, evidence-grounded PR input. It does not evaluate conflicts, call an LLM, persist jobs/results, publish Checks, or expose UI/API endpoints.

## Entry point and integration

`await ArchitectureContextBuilder(graph, source, readiness, repository_lock, settings).build(change_set)` returns `ArchitectureContext`. The existing `PullRequestChangeSet` is the input; no alternative PR ingestion pipeline was introduced.

- `Neo4jArchitectureGraphReader(driver, database)` provides repository-scoped read transactions. It creates no indexes or graph records.
- `GitRevisionSourceReader(repository_id, clone_root, PullRequestGitReader(repo_source, pr_settings))` reuses the bounded object/blob reader. It performs no fetch, checkout, reset, or ref update; exact objects must already be present from PR ingestion/indexing.
- `BaselineReadiness.snapshot(repository_id)` must return an authoritative `BaselineSnapshot` under the same mutation lock. This is a required injected port, with no permissive production default.
- Pass `RepositoryIngestionService._get_lock` as `repository_lock` to use the current full-name lock registry. Call the builder after PR ingestion releases that lock; do not recursively acquire it. Graph/ADR writers participating in production materialization must cooperate with the same registry.

Illustrative wiring, after services and a trustworthy readiness provider exist:

```python
graph = Neo4jArchitectureGraphReader(driver, configured_database)
object_reader = PullRequestGitReader(repo_source, pr_settings)
source = GitRevisionSourceReader(
    repo.id,
    repo_source._repo_clone_path(repo_source.resolve_ref(repo.full_name)),
    object_reader,
)
builder = ArchitectureContextBuilder(
    graph=graph,
    source=source,
    readiness=durable_readiness_provider,
    repository_lock=ingestion_service._get_lock,
)
context = await builder.build(change_set)
# All graph/Git materialization has completed and the lock is released here.
```

## Readiness dependency on Phase 4.3

The provider must validate repository identity/tracked branch, completed indexing, exact target-base SHA, graph generation, ingestion run identity, absence of unfinished indexing, and explicit READY state. DIRTY, UPDATING and UNKNOWN are rejected. A second readiness snapshot must agree with the first after materialization.

Do not implement the provider as `READY if indexed_commit_sha exists`. Existing graph writes are incremental and non-atomic, and SQLite does not yet have authoritative dirty/generation tracking. Phase 4.3 must implement that coordination and recovery. ADR updates also need to participate in snapshot synchronization; today their writers do not automatically use the indexing lock. No application startup wiring was added that would hide these missing guarantees.

The lock is retained if the materialization thread outlives async cancellation. A context can safely be used after the lock is released because every nested public collection is a tuple and every public contract is frozen. Readiness safety remains the provider/writer coordination responsibility, not an assertion inferred from a graph query.

## Evidence and mapping

Patch hunks are validated against available ingested contents. Complete before/after text yields precise deterministic changed ranges even without a patch. Old PR text is tagged MERGE_BASE; target blobs are independently read at `base_sha`; proposed text is PR_HEAD. Divergence and unavailable target data are explicit limitations. Evidence IDs and context hashes are deterministic; excerpts have independently verified hashes.

File lookup uses indexed `File.key` and `HAS_CHUNK`/`MENTIONS`. Code chunks are placed only by unique exact text in their exact creation-commit Git blob. Current code-backed relationship provenance additionally requires that chunk text to be uniquely corroborated in target-base source. Historical placement alone does not make an assertion current.

Python AST declarations and local route decorators refine mappings to CHANGED_SYMBOL. A chunk that overlaps changed code but mentions several unrelated abstractions remains ambiguous; rename-only entities remain FILE_CANDIDATE. New declarations receive context-local IDs with NEW_CANDIDATE origin; absence of a mapped identity is not proof of absence from the whole repository. No accepted graph data is modified.

Directed relationships retain source, target, assertion identity and supporting evidence. Code and ADR assertions are never merged solely because the source/type/target triple matches. Unsupported direct edges remain UNKNOWN with limitations. Dependency traversal is bounded to one hop by default, at most two, and ADR assertions do not drive further dependency propagation. Original ADR targets retain AFFECTS/CONSTRAINS direction in requirement target links.

## ADR discovery

Direct retrieval finds entity-targeted decisions/constraints, knowledge defined by ADRs whose chunks mention seeds, and persisted entity/community/repository/language scope hints. A second bounded catalog retrieval ranks lexical overlap with changed symbols, entity names, paths and delta snippets. Missing, uncertain, invalid, and mismatched scope stays eligible for fallback even without lexical overlap. Conditions supply positive discovery signals; a mismatch never proves irrelevance. This discovery does not establish applicability: every requirement has UNASSESSED PR applicability and UNKNOWN policy status for Phase 4.2.

Supporting text is retrieved through defining ADR and matching `adr_id`/`HAS_CHUNK`/`SUPPORTS` provenance. Original document/chunk hashes, section and source name/type/URL/version are retained where present. Corrupt supporting hashes are omitted with an explicit limitation. RKG_ONLY requires a successful, empty ADR catalog read. Failed/inconsistent retrieval yields RKG_ADR_DEGRADED; it never masquerades as confirmed absence.

### Persisted scope enrichment

`RequirementCandidate.declared_applicability_json` preserves the existing `ApplicabilityMetadata` envelope. Its `declared_applicability` accessor validates and returns a defensive copy of that same ADR model, including semantic scopes/targets, conditions, exceptions, qualifiers, scope basis, evidence references, schema/prompt/resolution versions, resolved IDs, graph snapshot, and uncertainty. Keeping the validated envelope as JSON avoids mutable enrichment lists entering the frozen context; no second enrichment schema was introduced. Missing/invalid metadata remains explicit through `scope_metadata_status`.

`supported_community_ids` requires repository-scoped actual MEMBER_OF associations, supported semantic names/roles/aliases, a whole-scope target, supported target basis and verified original ADR evidence. This uses `ADRScopeResolver.resolve` from enrichment. Affecting or mentioning an entity does not promote its requirement to community-wide scope. Declared/resolved IDs are preserved as retrieval hints even when their present association cannot be verified.

Original cross-section scope evidence is loaded independently of the requirement's SUPPORTS edges, using explicit metadata chunk references, defining ADR identity and source hashes. `scope_evidence_ids` distinguishes that support while also retaining it in ordinary evidence references. Neither SUPPORTS nor any accepted graph relationship is widened.

Each observed requirement's applicability properties are fingerprinted, including its validated envelope, versions, model/status and association-refresh fields. Per-ADR aggregates are included in `ADRIdentity.applicability_fingerprint`, `adr_catalog_hash`, and the context hash, including observed candidates later omitted by relevance/token limits. These are bounded observed-catalog fingerprints; retrieval truncation remains a limitation. Context builder version is now 2. ADR content hashes alone no longer identify enriched scope state.

## Limits

`ContextSettings` is injectable, or reads `DG_CONTEXT_<FIELD_NAME>` environment settings. Defaults: 80 entities, 160 relationships/chunks, 160 community associations and scope evidence chunks, 12 decisions and 12 constraints, 500 catalog candidates, 100 ADR documents, 40 deltas per file, 6,000 characters per evidence snapshot, 24,000 context budget units and one dependency hop.

The default token counter uses a conservative UTF-8 byte envelope, bounded below by the existing rough token estimator. This deliberately reserves more space than chars/4 and avoids tokenizer downloads or model dependencies. A caller may inject a model-specific tokenizer counter. Budget accounting covers the entire serialized context, including provenance and coverage metadata. Whole candidate units are pruned with limitations; dangling evidence/endpoint references are forbidden. If even revision/snapshot/file-coverage metadata cannot fit, construction rejects the budget rather than silently losing paths.

Use `ModelTokenCounter` to identify the actual model estimator in the context:

```python
counter = ModelTokenCounter.from_encoding(
    already_loaded_encoding, model=actual_evaluation_model, reserve_tokens=256
)
builder = ArchitectureContextBuilder(
    graph, source, readiness, repository_lock, token_counter=counter
)
```

Alternatively supply a named `ModelTokenCounter(name, count_tokens)` callback. The package does not resolve model names, download tokenizers, or call a model. Serialized budget accounting iterates to a safe bound, rejects invalid counts, and retains the existing configured limit. A complete patch, or complete text comparison reconstructing it, is successful coverage and is not reported as `PATCH_COMPLETE` limitation.

Phase 4.2 must interpret coverage and provenance before making conclusions. Exact source placement verifies text/location; it does not prove an LLM-extracted graph relationship is semantically correct. Non-Python symbol analysis, semantic ADR fallback, full merge simulation, multi-worker locking and durable publication are outside this phase.
