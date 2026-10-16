# Phase 1 Stage B: Prototype-Critical Production Fixes

## 1. Commits and modified files

Baseline: integration/production at 419ff737855b49031b6f9e7d309fe5c44e2fa746.
Backup branch: backup/production-before-ingestion-stage-b-20261016.
Implementation milestone: 88f1c03, Implement durable repository ingestion with fenced graph publication.
The second milestone adds real database tests, closes standalone maintenance mutation bypasses and records this handoff. Its hash is supplied in the final response; this document cannot embed its own commit hash.

No deployment, push, Session B merge, production database operation or processing-flag change was performed. Existing untracked reports were preserved.

Paths below are relative to backend unless stated otherwise:

- ai_services/ingestion/persistence/models.py
- ai_services/ingestion/persistence/database.py
- ai_services/ingestion/persistence/postgres_schema.py
- ai_services/ingestion/persistence/sqlite_store.py
- ai_services/ingestion/persistence/ingestion_jobs.py
- ai_services/ingestion/persistence/migrate_ingestion.py
- ai_services/ingestion/sources/github.py
- ai_services/ingestion/service.py
- ai_services/ingestion/worker.py
- ai_services/ingestion/dispatch.py
- ai_services/ingestion/graph_control.py
- ai_services/ingestion/readiness.py
- ai_services/ingestion/rkg/neo4j_writer.py
- ai_services/ingestion/community/detector.py
- ai_services/ingestion/community/membership.py
- ai_services/ingestion/community/pipeline.py
- ai_services/ingestion/community/projection.py
- ai_services/ingestion/community/vector_index.py
- ai_services/ingestion/adr/guard.py
- ai_services/ingestion/adr/neo4j_writer.py
- ai_services/ingestion/adr/applicability_store.py
- ai_services/runs/run_repository_ingestion.py
- ai_services/runs/ingestion_recovery_service.py
- ai_services/graph/service.py
- ai_services/graph/knowledge.py
- ai_services/decision_guard/orchestration/runtime.py
- ai_services/agents/rag_agent/agent.py
- ai_services/agents/rag_agent/tools/query_tool.py
- api_services/app/main.py
- api_services/app/models/repository.py
- api_services/app/routers/repositories.py
- api_services/app/routers/webhook.py
- api_services/app/utils/github_app_access.py
- api_services/app/utils/ingestion.py
- ai_services/tests/test_durable_ingestion.py
- ai_services/tests/test_ingestion_postgres.py
- ai_services/tests/test_ingestion_neo4j.py
- ai_services/tests/test_github_app.py
- ai_services/tests/test_webhook.py
- ai_services/tests/test_query_graph_context.py
- PRODUCTION_INGESTION_STAGE_B_REPORT.md at repository root

## 2. Full and incremental ingestion

Accepted executions carry an exact 40-character target SHA. Manual acceptance resolves the tracked branch using fresh installation authorization. Push acceptance persists the signed payload's after SHA. Neither can silently substitute a later HEAD under the same run identity.

The worker reconstructs an isolated temporary repository, fetches exact target and base objects, checks out the target detached, and validates the resulting SHA. Git credentials remain in subprocess environment headers. Git work drains before cancellation cleanup; working directories and local audit/candidate files are deleted on exit.

Missing base objects trigger full reconciliation at the accepted target. A missing target fails that execution. Normal incremental pushes use the published base SHA and only extract added/modified eligible files. Diff parsing is bounded, NUL-delimited, handles renames as deletion/addition, and includes file type changes.

Before processing and publication, the worker checks the current tracked branch. Stale accepted targets are marked SUPERSEDED and separate current-target work is persisted. This prototype uses simple live freshness checks, not a distributed event-ordering framework. A later push can occur after a check; accepted subsequent work converges the graph. Exclusive repository publication prevents an older worker overwriting an already published newer owner.

Direct legacy RepositoryIngestionService execution with the real persistent source now fails closed. Production PostgreSQL writes through the old unconditional start/success/failure methods are rejected; the worker adapts those calls onto its existing claimed run.

## 3. Durable execution and retry behavior

IngestionRunModel remains the history and execution identity. Added columns hold attempts, next-attempt time, owner, fencing token, lease expiry, full-repair flag, branch and installation. Added repository_ingestion_state holds one ownership/readiness barrier per repository. ingestion_deliveries maps webhook delivery IDs to existing runs. No extraction-artifact table, provider ledger, GCS response cache or duplicate history system was added.

Acceptance commits QUEUED work before HTTP 202. The response-local task only accelerates remote launch; losing it cannot lose the SQL intent. A private reconciler scans queued/retry/expired work and launches the existing Cloud Run Jobs transport with the run ID.

States are QUEUED, INDEXING, RETRY, COMPLETED, FAILED and SUPERSEDED. Initial limits are a 120-second lease, 20-second heartbeat and three attempts with bounded backoff. Expired/failed work requires full reconciliation. Provider calls may repeat on retry.

Duplicate signed deliveries return their prior run; reused delivery IDs with another repository/target are rejected. Matching active targets coalesce within a trigger. Manual sync/retry remains available. The sync request flag is retained for compatibility but now returns a durable accepted run rather than running indexing inside the HTTP request.

## 4. PostgreSQL and Neo4j ownership guarantees

Claims serialize on the repository SQL row. PostgreSQL's clock governs live claims and heartbeat checks. Only one active unexpired owner can claim a repository. Completion verifies current run, owner, token, target, installation, branch and lease. Stale SQL completion cannot regress indexed_commit_sha.

Every authoritative graph data transaction obtains a write lock on RepositoryGraphState, then checks token, owner, run, BUILDING state and Neo4j-side expiry under that lock. Ownership transfer advances the token under the same lock. Sealed tokens cannot mutate.

An already running old transaction can finish before the takeover acquires the graph lock. Reads stay closed throughout that interval, and the replacement performs full reconciliation. Once the new token is armed, stale transactions cannot begin authoritative mutations. SQL ownership alone is never treated as graph fencing.

Production standalone/maintenance entry points in the code writer, ADR writer, applicability store and community store reject unfenced drivers. They must run through the owned worker/ADR adapter; running an old maintenance script against production is no longer an implicit bypass.

## 5. Full reconciliation and recovery

Full reconciliation idempotently removes repository-scoped code chunks/files, then removes unsupported code assertions/direct edges and unreferenced entities. It leaves ADR chunks, requirements, ADR assertions and surviving shared entities/links intact, then reconstructs code evidence from fresh pinned Git and Azure processing.

No database is reset and no graph generation is copied. During incomplete work, SQL remains BUILDING or REPAIR_REQUIRED. A failed repair does not become READY just because an older indexed SHA exists.

Publication validates code assertion provenance and required community summary/vector presence, seals Neo4j COMPLETE for the run/target/token, and then conditionally publishes SQL. If SQL publication fails after the seal, the next invocation verifies the matching immutable seal and completes SQL without rerunning extraction. If ownership has advanced, recovery cannot publish the old token.

Historical legacy INDEXING rows do not permanently poison readiness after successful new-protocol reconciliation; readiness uses the explicit published run/token and active repository state. Existing repositories without new ownership state start unavailable until reconciled. Legacy producers must be stopped/disabled before enabling new producers.

## 6. Incremental GraphRAG fixes

- Unsupported code assertions and their unsupported direct edges are removed together.
- Direct relationships with surviving assertion/evidence support remain.
- Entity cleanup executes independently of assertion-list cardinality.
- A canonical entity with surviving chunk evidence retains its properties/vector; the changed-file batch can merge aliases without replacing its shared properties.
- Full reingestion removes obsolete code files/chunks rather than appending new commit chunks indefinitely.
- Required community configuration or processing errors fail ingestion.
- GDS uses repository/attempt-specific projections and stream assignments, eliminating shared temporary-property readback.
- Community membership and IDs use canonical entity IDs. Repository filtering prevents cross-repository assignments.
- Repository-wide community detection continues after an incremental change; hash-based summary/vector reuse is preserved.
- Concurrent creation of the shared community vector index verifies and reuses the winner instead of failing on an equivalent-index race.

Property preservation is conservative: this prototype does not reconstruct per-property provenance from every historical extraction. Reconciliation can refresh code-only properties; ADR/shared supported properties are intentionally retained.

## 7. ADR preservation

ADR knowledge stays in the same graph. Cleanup preserves ADR support and applicability links, including entities no longer supported by code. ADR graph persistence remains an explicit transaction.

The API wraps the ADR processor with the same SQL owner, epoch and Neo4j fence as code ingestion. Overlapping code/ADR owners cannot mutate concurrently. Successful ADR mutation refreshes required communities and publishes a new epoch. A failed/interrupted ADR update leaves the graph unavailable until recovery/reconciliation.

ADR ownership attempts reuse ingestion history with an ADR trigger. Durable recovery can repair baseline graph consistency, but does not automatically replay an uploaded document from a durable ADR job; failed ADR uploads retain the existing manual retry behavior. No broad ADR queue/refactor or ADR rebuild on every code push was introduced.

## 8. Graph read barrier

GraphReadBarrier checks fresh SQL READY state/epoch/published identity against the Neo4j COMPLETE seal before materialization and rechecks after it. Any changed epoch or dirty state rejects the result with temporary-unavailable HTTP 503.

Coverage:

- PR architectural context: production DatabaseReadiness and existing before/after context checks.
- Conversational GraphRAG: whole authorized query plus tool retrieval guards.
- Chat checkpoint identity: includes graph epoch, preventing reuse of evidence from another published epoch.
- Repository visualization, knowledge list/detail and graph-derived overview/risk enrichment.
- Derived PR retrieval indexes inherit the authoritative baseline epoch identity.

This is optimistic materialization with a final validation, not an assertion that Neo4j read-committed transactions provide snapshot isolation. Results from an inconsistent interval are discarded. Graph reads are deliberately unavailable while mutation or repair is incomplete.

## 9. Tests and evidence

Validation is separated by dependency:

- Existing/focused offline backend regressions: **429 passed, 0 failed, 1 skipped**, in 128.35 seconds. The skipped test requires Windows symlink creation, unavailable on this host.
- Real PostgreSQL 17.4: additive legacy-schema migration, idempotent rerun, row preservation, two independent Python process claims, takeover and stale completion rejection: **1 passed, 0 failed**, in 7.02 seconds.
- Real Neo4j Community 5.26.0 with GDS 2.13.2 on isolated localhost: **8 passed, 0 failed**, in 21.79 seconds on the final guard rerun.
- End-to-end graph worker uses real Git, SQLite lifecycle and real Neo4j/GDS, with deterministic fake Azure extraction, validation, summaries and embeddings. It proves full ingestion, incremental-only extraction, unchanged evidence preservation and recovery after an injected SQL publication failure without repeating extraction.
- Final focused Mock/SQLite rerun: **14 passed, 0 failed**, in 11.99 seconds. Covers queue durability, duplicate deliveries, bounded retries, stale owners, abandoned runs, dirty/changed epochs, worker crash recovery and repeated hard-death exhaustion exposing FAILED/manual retry.
- Real graph tests cover unsupported-edge deletion, surviving support, ADR preservation, full reconciliation, stale/expired owners, partial-write repair, concurrent repository GDS isolation and summary/vector reuse.

Logs and JUnit XML are under ignored .integration-validation/stage-b-*. The PostgreSQL test refuses existing schemas and non-local URLs. Neo4j tests refuse any endpoint other than bolt://127.0.0.1:57687 and use unique stage_b_ repository IDs. No production connection or live Azure/GitHub call was used. Both isolated database listeners were stopped after validation; test data was retained.

## 10. Required configuration and resources

Nothing in this section has been applied to GCP.

1. Explicit additive PostgreSQL upgrade: from backend, python -m ai_services.ingestion.persistence.postgres_schema --upgrade. Startup remains validation-only for PostgreSQL. The ingestion marker and additional fields require a matching new application binary; old binary rollback is not assumed compatible.
2. Neo4j/GDS compatible with the tested graph procedures, writable constraints/indexes, and the RepositoryGraphState uniqueness constraint. The worker initializes required schema; required failures block publication.
3. Baseline Cloud Run Job using the same backend container, one task per execution, with python -m ai_services.runs.run_repository_ingestion. Supply INGESTION_RUN_ID through launch overrides; start with platform retries disabled because SQL owns bounded retries.
4. Set INGESTION_JOB_RESOURCE to projects/PROJECT/locations/REGION/jobs/JOB on API/recovery service. Their identities need permission to run that Job with overrides.
5. Private recovery service command: uvicorn ai_services.runs.ingestion_recovery_service:app --host 0.0.0.0 --port 8080. Require Cloud Run IAM authenticated invocation; expose only POST /reconcile.
6. Scheduler authenticated invocation of /reconcile, initially every minute. Session B can later compose baseline reconciliation into its private recovery service without holding the long evaluation semaphore.
7. Worker uses shared PostgreSQL, Neo4j/GDS, GitHub App installation credentials and the existing Azure model/embedding configuration. Use secret references and existing service identities, not credentials in commands.
8. Configure Job memory, CPU and task timeout from representative ingestion measurements; full GraphRAG may exceed the HTTP Cloud Tasks deadline. Cloud Run Jobs is the chosen long execution model.

ENABLE_BACKGROUND_PROCESSING was not changed. Enabling API/worker/recovery processing, cloud migration, resource provisioning and deployments require the separately authorized production phase. No GCS bucket is required for baseline ingestion; Session B's ADR-original storage remains separate.

## 11. Remaining risks

- No live Azure, GitHub App, Cloud Run IAM, Job launch or Scheduler validation was performed.
- No production-sized ingestion benchmark exists; timeout/memory/concurrency sizing remains a deployment prerequisite.
- Repository graph reads pause through extraction, mutation and repair. This is the accepted in-place prototype tradeoff.
- Retries can repeat provider calls. There is no exactly-once provider execution or durable response cache.
- Different repositories can execute concurrently; no new global admission/cost framework was added. Size operational concurrency to SQL/Neo4j/Azure capacity.
- Conservative shared entity property preservation can retain old descriptive properties until reconciliation; it avoids the confirmed partial-batch overwrite.
- Old unfenced application revisions and external scripts must remain disabled before processing enablement. New code cannot fence an old binary that never consults the control node.
- Existing graph data is not automatically certified. Perform controlled full reconciliation before reopening graph reads.
- Private recovery endpoint relies on Cloud Run IAM, matching Session B's pattern; deploy it without public invocation.
- Session B integration still needs combined regression tests. Its overlapping tracked files are app/main.py and ingestion/persistence/sqlite_store.py. Preserve the new read/ADR guards while integrating its dispatcher and original-storage changes.

## 12. Session B merge decision

GO for a controlled Session B code merge and combined validation after reviewing these commits. The baseline callback-loss, abandoned-run poisoning and stale-publication blockers now have implemented and tested ownership/recovery paths.

NO-GO for production processing enablement until the additive migration, resources/IAM, old-writer isolation, representative sizing and deployed smoke tests are approved and verified. This task did not merge Session B or deploy.
