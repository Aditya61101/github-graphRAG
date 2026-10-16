# Session B controlled integration

Date: October 17, 2026. Branch: `integration/production`.

Combined local validation passed. The integration preserves both processing paths and graph ownership safeguards. Production enablement remains gated by the outstanding checks below.

## Inputs and rollback point

- Stage B integration HEAD: `5ba0bbf1d2a874d210ffb14433fb89125a98cf8a`, including `88f1c0315e05a0546564368aad86ac9419fba111`.
- Session B: `051a1a5c222309feed23fdd3356eb0c3ee8009bc` from `feature/production-processing`. The separate processing worktree was not modified.
- Verified shared merge base: `419ff737855b49031b6f9e7d309fe5c44e2fa746`.
- Created `backup/production-before-session-b-20261017` at the original Stage B HEAD before merging.
- Used a non-fast-forward merge. Git found no textual conflicts; both overlapping files received a semantic review.
- The final merge commit is identified in the delivery message and by `git log --merges -1 integration/production`. This report is included in that merge.

No deployment, push, production migration, database reset, cloud resource modification or provider switch was performed. No persistent processing setting was enabled. Synthetic test fixtures opt in only inside isolated test processes. Azure OpenAI remains the configured production provider.

## Preserved behavior

| Area | Integration evidence |
| --- | --- |
| Repository ingestion | `ingestion/persistence/ingestion_jobs.py`, `ingestion/dispatch.py`, `ingestion/worker.py` and `runs/run_repository_ingestion.py` retain durable SQL acceptance, owner/epoch leases, direct Cloud Run Jobs launch and temporary pinned Git workspaces. Cloud Tasks was not added to this path. |
| Graph ownership | `ingestion/graph_control.py`, `ingestion/readiness.py` and `ingestion/adr/guard.py` remain in place. SQL publication, Neo4j mutation fences, seals and read barriers are retained. |
| API startup overlap | `api_services/app/main.py` retains `GraphReadBarrier`, graph/RAG guards and `GuardedADRProcessor`; Session B adds the configured PR dispatcher. In-process PR recovery starts only for the local dispatcher. |
| SQL-store overlap | `ingestion/persistence/sqlite_store.py` retains Stage B ownership protections. Session B's failed-ADR retry preserves the existing ADR ID and original location instead of adopting a fresh provisional ID's path. |
| PR evaluation | `decision_guard/orchestration/gcp.py` sends Cloud Tasks to authenticated HTTP `/evaluate`; deterministic time-bucket task names, OIDC audience, 1800-second dispatch deadline and duplicate-task handling are preserved. `runs/evaluation_service.py` retains terminal-only acknowledgement and evaluation timeout behavior. |
| SQL outbox and recovery | Existing evaluation jobs, dispatch intents, attempts, leases, lifecycle reconciliation and publication recovery are preserved. Session B's PostgreSQL advisory lock enforces global PR admission across processes. |
| Originals and authority | `ingestion/adr/originals.py`, `ingestion/adr/service.py` and `decision_guard/evaluation/authority.py` retain GCS original provenance, conditional object creation, scoped/bounded reads and content-hash verification. Originals are stored before graph processing, which still uses the guarded ADR adapter. |
| GitHub Checks | Existing current-head checks, durable publication state, retry handling and the explicit Check-publication enable flag remain. |
| Disabled production processing | `shared/utils/processing.py` defaults processing off for Cloud Run service/job and production environment markers. Explicit false remains honored by both workers and recovery. |

Paths in this table are relative to `backend/ai_services/` unless starting with `api_services/` or `shared/`, which are relative to `backend/`.

## Combined recovery and scheduling

The private `runs/evaluation_service.py` application now reconciles both queues at `POST /reconcile`:

1. PR lifecycle, evaluation dispatch intents and GitHub Check publication use the existing `EvaluationOrchestrator` and Cloud Tasks HTTP transport.
2. Repository ingestion uses `IngestionJobs` and the existing direct Cloud Run Jobs dispatcher.

The two components run independently with `asyncio.gather(..., return_exceptions=True)`. An uncaught component failure returns a sanitized HTTP 503 naming only the failed component, after both have been attempted. Per-job dispatch failures retain each queue's existing durable retry behavior and sanitized logging. A successful sweep means reconciliation ran; it does not assert every provider dispatch succeeded.

Recovery uses a separate semaphore from the long-running `/evaluate` route. The legacy `runs/ingestion_recovery_service.py` remains available for baseline-only operation, including configurations without an ADR bucket. The combined endpoint requires shared PostgreSQL and the PR/GCS configuration.

### Prepared scheduling change; not applied

Use one authenticated every-minute Scheduler target for the combined endpoint. Replace an existing baseline-only schedule when moving to this target; do not intentionally run duplicate schedules. Deploying resources and creating/updating this schedule require the separately approved production phase.

Application command:

```text
uvicorn ai_services.runs.evaluation_service:app --host 0.0.0.0 --port 8080
```

Prepared PowerShell template, with project-specific values supplied during production review:

```powershell
# PREPARED ONLY: do not execute without production approval.
gcloud scheduler jobs create http decisionguard-processing-recovery `
  --project $ProjectId --location $Region `
  --schedule '* * * * *' --time-zone 'Etc/UTC' `
  --uri "$RecoveryOrigin/reconcile" --http-method POST `
  --oidc-service-account-email $SchedulerInvoker `
  --oidc-token-audience $RecoveryOrigin
```

For an existing job, prepare `gcloud scheduler jobs update http` after reviewing its current target and identity. Keep the service private with IAM-authenticated invocation. Do not enable processing as part of merely deploying the disabled application.

Prefer a dedicated recovery service using the same private app/image so long evaluation requests cannot consume its HTTP capacity. A shared service needs sufficient request/instance capacity as well as the separate application semaphore. Review Cloud Run request timeout, queue concurrency and worker instance sizing against the 1680-second evaluation timeout and 1800-second task deadline. No sizing or IAM claims are established by local tests.

Required production configuration includes the shared PostgreSQL database, `ADR_GCS_BUCKET`, the PR queue/worker URL/task invoker identity, `PR_EVALUATION_DISPATCHER=cloud_tasks` and `INGESTION_JOB_RESOURCE`. Repository ingestion needs its own direct Job-launch permissions; it does not need a Tasks queue. Keep `ENABLE_BACKGROUND_PROCESSING=false` until the separately approved enablement phase.

## Validation evidence

Local logs and XML results are retained under the ignored `.integration-validation/` directory. They contain synthetic test identities, not production credentials.

| Validation | Result |
| --- | --- |
| Full offline backend, first run | 1,113 passed, 24 skipped, 4 failed; all four failures reproduced unchanged at committed Stage B `5ba0bbf`. See explanation below. |
| Final full offline backend | **1,121 passed, 25 skipped, zero failures**, in 571.95 seconds. Includes all 17 Session B tests and all 4 new combined recovery tests. |
| Updated-contract and combined recovery regression run | All 81 tests passed across PR ingestion, repository monitoring permissions, retrieval and combined recovery. |
| Session B transport, original storage and private worker tests plus graph suite | 29 passed initially; the new ADR test had a missing required synthetic repository ID. Its corrected rerun passed. Total coverage: 17 Session B tests, 4 combined recovery tests and 9 real Neo4j tests. |
| Real PostgreSQL legacy migration and ingestion ownership | Passed in a new empty database; upgrade run twice, schema validated and historical user/run records preserved. Competing independent processes produce one ingestion owner; takeover fences stale publication. |
| Expanded real PostgreSQL combined admission | Passed in another fresh database; both queues coexist after migration, PR outbox intents persist, independent PR workers honor a global concurrency limit, expired PR ownership cannot heartbeat and historical ingestion publication remains preserved. |
| Real PostgreSQL PR claims/retries | Passed in a separate fresh database; idempotent upgrades, duplicate event coalescing, competing claims and failed-revision retries verified. |
| Real Neo4j/GDS | Nine tests passed across the initial run and corrected ADR rerun, against isolated Neo4j Community 5.26.0/GDS 2.13.2. Covers stale/expired owner rejection, supported evidence preservation, scoped GDS, incremental Git ingestion and recovery after sealed-graph/SQL publication failure. |
| Combined GCS/ADR ownership boundary | Passed using mocked GCS HTTP transport and real ADR transactional writer, SQL ownership, Neo4j fencing and read barrier. Confirms original-before-graph ordering, retained `gs://` provenance, correct fenced writer/resolver and rejection after seal. Community processing is stubbed in this boundary test; real GDS is covered separately. |
| Frontend | TypeScript `--noEmit`, TypeScript build, Vite production build and all 134 frontend tests passed. |
| Patch hygiene | `git diff --check` and staged equivalent passed. |
| Source preservation | Direct comparisons confirm Stage B's ingestion lifecycle/models/migrations, graph control/readiness, ADR guard, worker, dispatch and Git source files are unchanged. Session B's authority, Tasks transport, evaluation store, originals/service/models, PR runner and processing-gate files match `051a1a5` exactly. |

PostgreSQL 17 tests were restricted to localhost port 55439 and explicitly named new test databases. They refuse existing tables before migration. Neo4j tests were restricted to localhost port 57687 and unique synthetic repository identities. No database was reset or overwritten.

Both local test listeners were verified closed after validation; their stored synthetic data was retained.

### Four pre-existing test failures repaired

An untouched archive of `5ba0bbf` reproduced all four failures, independently of Session B:

- `test_pr_ingestion.py::test_additive_pr_schema_initialization_preserves_repository_data` omitted the core `ingestion_runs` table. Its legacy SQLite fixture now includes that required table.
- `test_repository_monitoring_permissions.py` had two success assertions requiring synchronous calls to the old ingestion service. They now verify the accepted durable `QUEUED` run and confirm ingestion is not executed inside the API request; permission-denial assertions remain intact.
- `test_reranking.py::test_tool_keeps_metadata_out_of_prompt_and_uses_effective_query` omitted the mandatory graph readiness barrier. It now supplies a scoped fixture barrier and verifies its repository scope. The real barrier remains required by production code.

Only tests were changed for these four failures. No guard or ownership check was relaxed.

### Validation limitations and warnings

- `test_gemini.py` and `test_neo4j.py` are import-time live-provider connectivity scripts, not offline tests. They were excluded to avoid external provider/database access. The actual isolated Neo4j/GDS suite was run separately. No Gemini switch or invocation was performed.
- Skips include unavailable private/historical Case A evidence, optional replay artifacts, Windows symlink creation and the opt-in database tests in the offline run. The database tests were run separately against isolated instances.
- The first sandboxed frontend run timed out in Vite SSR transport. The unrestricted local rerun passed all 134 tests. Vite reports the existing large-chunk warning; dashboard tests emit an existing duplicate `/settings` React key warning. Frontend source was not changed by the merge.
- Docker/container build and deployed image validation were not performed. The existing backend manifest and lock already include the Google auth/requests dependency chain used by Session B; that source review is not a container build result.

## Remaining production blockers and missing tests

This integration is not approval to enable production processing.

1. The production backup, additive schema rehearsal using an isolated copy of the actual production schema, approved migration and deployed smoke tests remain separate production gates. Synthetic PostgreSQL migration success does not replace those gates.
2. Cloud Tasks queue, private worker/recovery IAM, OIDC invocation, GCS access, Scheduler delivery, direct repository Job launch and actual Azure/GitHub behavior have not been exercised live. Authenticated deployed end-to-end tests and representative timeout/load tests remain necessary.
3. Stop or isolate old unfenced graph writers before new writers are enabled. Repositories without verified SQL ownership state and a matching Neo4j seal remain unavailable until reconciled.
4. Existing local ADR originals are not automatically copied to GCS. Verify/migrate provenance through a separately reviewed procedure before remote evaluations rely on those records. Missing originals fail closed.
5. ADR uploads still use the synchronous guarded processor. An interrupted upload is not automatically replayed as a durable ADR document job; graph consistency can be repaired through ingestion recovery and document retry remains manual. GCS originals do not add such a replay system.
6. Full recovery under deployed instance loss, IAM/provider outages, representative large repositories and real Azure costs remains unmeasured. No claim of production readiness follows from local transport mocks.

No unresolved correctness issue requiring weaker graph fencing was accepted.
