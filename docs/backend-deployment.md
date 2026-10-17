# Backend production deployment

The manual workflow `.github/workflows/deploy-backend.yml` deploys only the commit
selected by a dispatch on `integration/production`. There is no push or PR trigger.
The existing frontend Firebase workflows, project, hosting and authentication stay
independent. Backend GCP project: `decisionguard-ai-builder-cup` (note that the
Firebase project spelling in the frontend workflows is different).

## Release behavior

1. Reject other branches, check out the dispatch SHA, install Python 3.13 and
   uv 0.12.5, install `backend/uv.lock`, and run the offline backend regression
   suite plus deployment safety tests. No production credentials enter this job.
   `test_neo4j.py` and `test_gemini.py` are legacy live scripts with network calls
   at import time, so they are excluded. Tests marked `integration` are deselected;
   opt-in local Postgres/Neo4j tests skip without their test-specific variables.
   `.env` loading is disabled and `JWT_SECRET` is a fixed, public CI-only test value.
   Existing regression failures block deployment.
2. Build the existing root Dockerfile **once** for linux/amd64, then obtain
   short-lived Google credentials through GitHub OIDC and a dedicated deployment
   existing deployment service account. The action mints a masked, one-hour account access token used
   by gcloud and its Docker credential helper, so rollout does not depend on the
   initial GitHub OIDC token remaining valid. Authentication happens after the
   build. No JSON service-account key is needed.
3. Push
   `asia-south1-docker.pkg.dev/decisionguard-ai-builder-cup/decisionguard/backend:git-SHA-run-ID-ATTEMPT`.
   Resolve the pushed tag in Artifact Registry to `backend@sha256:...`. Every
   selected target uses that same digest. Rerunning a workflow builds once again
   for that attempt; there is no rebuild between targets.
4. Preflight every selected existing resource. Require one container, Ready status,
   and (for services) one untagged revision with 100% traffic and no newer unserved
   or unready revision. Split traffic, sidecars, missing targets and pending rollouts
   fail before any Cloud Run update. Reconcile those layouts explicitly first.
   The API must already have one literal `PR_EVALUATION_SETTINGS` value. It is
   validated offline by the selected image's own `automatic_evaluation_settings`
   production admission validator before the worker is updated, and again after
   the API image update. Missing/invalid profiles, secret references, or images
   missing the canonical validator fail closed. No profile/budget defaults are
   copied into the deployment code. Complete the one-time transition below first.
5. Update `decisionguard-pr-evaluation-worker`, verify its ready revision's resolved
   image digest, and move 100% traffic to it. Only then update/verify
   `decisionguard-api` and check its canonical Cloud Run URL at `/health` for
   HTTP 200 and JSON `{"status":"ok"}`. The endpoint must already be reachable
   from the GitHub runner. This workflow does not grant public access or weaken
   ingress/IAM. A private API needs an approved runner/authentication design before
   using this workflow; the current check intentionally fails closed in that case.
6. If selected, update `decisionguard-processing-recovery`, then the
   `decisionguard-repository-ingestion` and `decisionguard-adr-ingestion` job
   templates. Jobs are **not executed**. Their configured image digests are checked;
   successful job execution is outside this release check.

Routine CI uses only `gcloud run services update --image`,
`services update-traffic --to-latest`, and `jobs update --image` to mutate Cloud Run.
No environment, secrets, Cloud SQL,
runtime identity, resources, concurrency, HTTP/task timeout, command/args, budgets,
queue, migration or IAM flags are passed by the workflow. The separately approved
one-time profile transition below is an operator action. The helper also compares the runtime
specification and configuration annotations before/after, excluding the image,
traffic and generated client/revision metadata. It does not print resource JSON.
No migrations are explicitly run by CI; existing application startup behavior is
unchanged, so deploy only schema-compatible commits.

Failures stop subsequent targets. There is no automatic rollback: an earlier target
can already be live, so inspect the release artifact and use the commands below.
The fixed concurrency group serializes backend production runs without cancelling
an active rollout. GitHub may replace an older pending run with a newer pending run;
this is mutual exclusion, not a FIFO deployment queue. Console/manual deployments
are outside that lock: avoid them during rollout. The helper checks for changes
since preflight, but cannot atomically lock other deployment tools.

`backend-release.json` is uploaded even after failure when available, for 90 days.
It records the commit, new digest, previous revision/image per target and rollout
progress. It contains no environment values or secret payloads. A failed preflight
can leave only a partial manifest; no Cloud Run updates occur until preflight ends.
The workflow summary records the commit and new digest. Keep known-good digests
and revisions for the desired rollback window; Artifact Registry cleanup policies
must not delete them.

## GitHub repository setup (administrator, once)

- Enable GitHub Actions and allow the actions used in this workflow: `actions/*`,
  `astral-sh/setup-uv`, and `google-github-actions/*`. The workflow grants only
  `contents: read`, with `id-token: write` added in the deployment job.
- Create environment **`backend-production`**. Restrict deployment branches to
  **`integration/production`** only; disallow tags. Configure required reviewers
  and prevent self-review if required by your production policy. Protect the
  production branch and require review of workflow/helper changes.
- Reuse these **existing repository variables**. Environment-scoped overrides,
  if any, must match; the workflow checks the expected identity before building:

  | Variable | Value |
  | --- | --- |
  | `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/542430141825/locations/global/workloadIdentityPools/github-actions/providers/decisionguard` |
  | `GCP_DEPLOY_SERVICE_ACCOUNT` | `decisionguard-github-deployer@decisionguard-ai-builder-cup.iam.gserviceaccount.com` |

- No backend GitHub secrets are required. Do not copy runtime secrets or GCP keys
  into GitHub. Leave the Firebase service-account secret and frontend settings alone.
- GitHub requires a `workflow_dispatch` workflow to exist on the repository's
  **default branch** for it to be discoverable/dispatchable. Review and land the
  same workflow, helper, tests and documentation on the default branch and on
  `integration/production`. Do not change the default branch just for deployment.
  Dispatch using the production ref; a default-branch dispatch fails the guard
  unless the default branch itself is `integration/production`.

## Existing Workload Identity Federation configuration

Reuse project number **542430141825**, pool **github-actions**, provider
**decisionguard**, and account
**decisionguard-github-deployer@decisionguard-ai-builder-cup.iam.gserviceaccount.com**.
They are already provisioned. Do not create a second pool, provider, deployment
account or service-account key. The workflow does not provision or change WIF/IAM.

Required APIs: IAM, IAM Service Account Credentials, Security Token Service,
Cloud Resource Manager, Artifact Registry and Cloud Run. No Cloud Build API or
Cloud Build account is required; Docker builds on the GitHub runner.

Before release, have the administrator verify that this existing provider trusts
the repository's numeric owner/repository IDs, the intended production branch,
and this workflow's OIDC claims, including environment `backend-production`.
If its existing condition restricts workflow paths/events, it must permit
`.github/workflows/deploy-backend.yml`, `workflow_dispatch`, and
`refs/heads/integration/production`. Confirm the repository principal already has
`roles/iam.workloadIdentityUser` on the existing account. Audit compatibility with
other consumers before any separately approved trust-policy change; do not
replace or narrow the shared provider automatically. Keep the provider's existing
audience configuration compatible with the auth action's provider-name audience.

## Deployment account permissions

Administrators provision these grants separately; the workflow never changes IAM.

| Principal | Grant and scope | Purpose |
| --- | --- | --- |
| Existing federated repository principal | `roles/iam.workloadIdentityUser` on existing deployment account | Exchange OIDC credentials for account impersonation |
| Deployment account | `roles/artifactregistry.writer` on repository `decisionguard` in `asia-south1` | Push/read image and resolve digest |
| Deployment account | `roles/run.developer` on each of the three existing services and two jobs | Read/update resources and read revisions; no IAM policy changes |
| Deployment account | `roles/iam.serviceAccountUser` on each existing runtime service account used by selected targets | Act as the existing runtime account during an image update |
| Deployment account | `roles/serviceusage.serviceUsageConsumer` on project, if required for quota project usage | Consume enabled APIs |

For example (administrator only), grant Artifact Registry writer with:

```bash
PROJECT=decisionguard-ai-builder-cup
DEPLOYER=decisionguard-github-deployer@decisionguard-ai-builder-cup.iam.gserviceaccount.com
gcloud artifacts repositories add-iam-policy-binding decisionguard \
  --project="$PROJECT" --location=asia-south1 \
  --member="serviceAccount:$DEPLOYER" --role=roles/artifactregistry.writer
```

Apply `roles/run.developer` using `gcloud run services add-iam-policy-binding NAME`
or `gcloud run jobs add-iam-policy-binding NAME`, with `--project`, `--region`,
`--member="serviceAccount:$DEPLOYER"` and `--role=roles/run.developer`. Grant
`roles/iam.serviceAccountUser` on each runtime account via
`gcloud iam service-accounts add-iam-policy-binding RUNTIME_ACCOUNT` with that
member and role. Inventory the existing runtime accounts; do not replace them.

Existing Cloud Run service agents must retain their existing Artifact Registry
reader access. Runtime service accounts keep their current Secret Manager,
Cloud SQL, Tasks and other runtime permissions. The deployment account does not
need secret payload access, database credentials, queue administration or job
execution permission. No Token Creator grant is required for normal WIF account
impersonation used here.

## One-time API profile transition (operator release prerequisite)

Coordinate this transition with
[the regression-fix runbook](../backend/PR_EVALUATION_REGRESSION_RUNBOOK.md),
especially its **Exact evaluation settings** and **Deployment steps** sections.
That runbook and `ai_services.decision_guard.orchestration.profiles` arrive with
the separate regression fix; they must be integrated into the selected commit
before release. This CI/CD branch deliberately does not copy its runtime changes.

The new API rejects production evaluation webhooks with HTTP 503 when
`PR_EVALUATION_SETTINGS` is missing or invalid. `/health` alone cannot detect this.
Do not deploy the new API image first and add the profile in a later revision.
Do not apply the profile alone to the old image to satisfy the CI guard. Stage
the new API image **and** the approved JSON in a single revision, validate that
actual revision, then promote it. The old API continues serving throughout staging.

The canonical Azure profile explicitly includes `o200k_base`, `DEFAULT`, 43 model
attempts, 1,075,000 tokens, 1500s evaluator deadline, concurrency 2, input/output
limits 20,000/5,000, model window 32,768 and reserve 1,024. These are an explicit
one-time operator approval of the regression runbook's policy, not defaults applied
by CI. Preserve existing unrelated non-default read, retry and cost settings.
Retain `PR_EVALUATION_DEPLOYMENT_REVISION` unless model configuration changes.
Workers consume persisted job settings; recovery and ingestion need no API profile.

Use **one already-built immutable combined-release digest** for worker and API.
If the workflow first encounters an absent profile, it builds/pushes its image but
fails preflight before changing any Cloud Run target. Its summary provides the
digest for this approved transition; there is no need to rebuild it. Alternatively
use the runbook's existing approved build process. Verify the image commit includes
both fixes and this deployment tooling. No commands below were executed here.

Example PowerShell, from the repository root after separate deployment approval:

```powershell
$project = 'decisionguard-ai-builder-cup'
$region = 'asia-south1'
$image = 'asia-south1-docker.pkg.dev/decisionguard-ai-builder-cup/decisionguard/backend@sha256:<APPROVED_NEW_DIGEST>'
$release = '<UNIQUE_LOWERCASE_RELEASE_ID>'
$profileFile = '<APPROVED_UTF8_PROFILE_JSON_FILE>'

# Prepare/review the profile using azure_production_settings() in the combined
# source, as in the regression runbook. Merge existing unrelated settings first.
# This uses the selected image's actual production validator, with no network:
python scripts/deployment/validate_api_profile.py --image=$image --profile-file=$profileFile
if ($LASTEXITCODE -ne 0) { throw 'Profile validation failed; do not deploy' }
$profileJson = (Get-Content -Raw -LiteralPath $profileFile | ConvertFrom-Json | ConvertTo-Json -Compress -Depth 30)
if ($profileJson.Contains('|')) { throw 'Choose a different gcloud delimiter before proceeding' }

# Confirm no split/tag traffic or concurrent deployments. Capture the currently
# serving revisions for rollback, without exporting environment/secret values.
$oldWorkerRevision = gcloud run services describe decisionguard-pr-evaluation-worker --project=$project --region=$region --format='value(status.traffic[0].revisionName)'
if ($LASTEXITCODE -ne 0 -or -not $oldWorkerRevision) { throw 'Cannot capture worker rollback revision' }
$oldApiRevision = gcloud run services describe decisionguard-api --project=$project --region=$region --format='value(status.traffic[0].revisionName)'
if ($LASTEXITCODE -ne 0 -or -not $oldApiRevision) { throw 'Cannot capture API rollback revision' }
$workerTimeout = gcloud run services describe decisionguard-pr-evaluation-worker --project=$project --region=$region --format='value(spec.template.spec.timeoutSeconds)'
if ($LASTEXITCODE -ne 0 -or $workerTimeout -ne '1800') { throw 'Reconcile worker timeout with regression runbook before release' }
# Preserve this timeout and all other component configuration.
gcloud run services update decisionguard-pr-evaluation-worker --project=$project --region=$region --image=$image --no-traffic --revision-suffix="$release-worker"
if ($LASTEXITCODE -ne 0) { throw 'Worker staging failed' }
$workerRevision = gcloud run services describe decisionguard-pr-evaluation-worker --project=$project --region=$region --format='value(status.latestCreatedRevisionName)'
if ($LASTEXITCODE -ne 0) { throw 'Worker inspection failed' }
$workerDigest = gcloud run revisions describe $workerRevision --project=$project --region=$region --format='value(status.imageDigest)'
if ($LASTEXITCODE -ne 0 -or $workerDigest -ne $image) { throw 'Worker digest mismatch' }
$workerReady = gcloud run services describe decisionguard-pr-evaluation-worker --project=$project --region=$region --format='value(status.latestReadyRevisionName)'
if ($LASTEXITCODE -ne 0 -or $workerReady -ne $workerRevision) { throw 'Worker revision is not ready' }
gcloud run services update-traffic decisionguard-pr-evaluation-worker --project=$project --region=$region --to-revisions="$workerRevision=100"
if ($LASTEXITCODE -ne 0) { throw 'Worker promotion failed; stop' }

# One update contains BOTH the new image and the validated profile. --update-env-vars
# preserves all other env/Secret Manager references; never use --set-env-vars.
gcloud run services update decisionguard-api --project=$project --region=$region --image=$image --update-env-vars="^|^PR_EVALUATION_SETTINGS=$profileJson" --no-traffic --revision-suffix="$release-api"
if ($LASTEXITCODE -ne 0) { throw 'API staging failed; old API remains serving' }
$apiRevision = gcloud run services describe decisionguard-api --project=$project --region=$region --format='value(status.latestCreatedRevisionName)'
if ($LASTEXITCODE -ne 0) { throw 'API inspection failed' }
# Inspect the actual staged revision privately, verify Ready/image/service, and
# validate its actual env value with the image's admission contract. No secrets
# or profile values are printed and no provider calls are made.
python scripts/deployment/validate_api_profile.py --image=$image --revision=$apiRevision --expected-profile-file=$profileFile
if ($LASTEXITCODE -ne 0) { throw 'Staged API validation failed; do not promote' }
gcloud run services update-traffic decisionguard-api --project=$project --region=$region --to-revisions="$apiRevision=100"
if ($LASTEXITCODE -ne 0) { throw 'API promotion failed; inspect traffic and rollback' }
$apiUrl = gcloud run services describe decisionguard-api --project=$project --region=$region --format='value(status.url)'
if ($LASTEXITCODE -ne 0) { throw 'API URL inspection failed' }
$health = Invoke-RestMethod "$apiUrl/health"
if ($health.status -ne 'ok') { throw 'API health failed; use revision rollback' }
```

Before routine CI resumes, confirm 100% traffic points at the validated revisions,
they are the latest ready/created revisions, no tags remain, and all existing
timeouts, runtime identities, secrets and other settings are intact. A failed staged
revision leaves the old API serving; reconcile or roll back before retrying CI.
Routine CI never sets or repairs the profile, budgets, queue settings or IAM.
Its Docker check uses `--network=none`, sends JSON via stdin rather than process
arguments, and suppresses container output; invalid/missing canonical validators
block rollout. Profiles backed by Secret Manager references fail closed without
resolving secret payloads; this non-secret policy JSON must be a literal env value.

PR #3 continuation, evaluation/provider calls, and Check publication remain separate
operator actions in the regression runbook. Deployment never performs them.

## Integration and manual release

1. Review the `feature/backend-cicd` commit. Integrate the concurrent PR evaluation
   regression fix independently; this change does not contain that fix.
2. In the normal integration checkout, bring the reviewed CI/CD commit onto
   `integration/production` with `git cherry-pick <backend-cicd-commit>` (or the
   team's reviewed PR process). Review deployment/fixture overlaps with the
   regression fix without overwriting its runtime changes. Run the
   offline tests below on the final combined production commit. Push the reviewed
   branch through the normal branch protection process.
3. Land the same deployment files on the repository default branch as described
   above. Verify the GitHub environment, existing WIF trust and scoped deployer grants.
   None of these integration/setup actions are performed by this implementation.
4. Complete the one-time profile transition below using the combined image and
   regression-fix runbook, then validate the live API revision. Routine dispatches
   intentionally refuse to bootstrap or change evaluation profiles/budgets.
5. Actions → **Deploy backend to production** → Run workflow → select
   **integration/production**. Optional switches default to false. Confirm the
   displayed branch SHA is the intended combined release, then approve the
   environment deployment if configured.

Equivalent CLI dispatch (operator only, after integration/configuration):

```bash
gh workflow run deploy-backend.yml --ref integration/production \
  -f deploy_recovery=false \
  -f deploy_repository_ingestion=false \
  -f deploy_adr_ingestion=false
gh run list --workflow deploy-backend.yml --branch integration/production
gh run watch RUN_ID --exit-status
gh run download RUN_ID --name backend-release-RUN_ID-ATTEMPT
```

The SHA is fixed when dispatched and used by both jobs; later branch pushes do not
change that run. Do not skip failing tests to deploy. If an update succeeds but a
later step fails, inspect the release manifest and current Cloud Run state before
retrying. A new run rebuilds the selected SHA and can complete a partial release;
preflight may require an operator to reconcile an unserved/unready revision first.

## Rollback (operator only)

Stop new dispatches and wait for any active rollout to finish before manual rollback.
Use `previous_revision` and `previous_image` from the release manifest, not a mutable
tag. Inspect partial states (`pending`, `updating`, `image-verified`, `verified`); a command may have
completed server-side even if the workflow timed out. Roll back only changed
targets. Preserve worker-before-API ordering unless incident response requires
otherwise. No workflow automatically runs these commands.

Fast service rollback to retained known-good revisions (no rebuild):

```bash
gcloud run services update-traffic decisionguard-pr-evaluation-worker \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --to-revisions=PREVIOUS_WORKER_REVISION=100
gcloud run services update-traffic decisionguard-api \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --to-revisions=PREVIOUS_API_REVISION=100
# If recovery was deployed:
gcloud run services update-traffic decisionguard-processing-recovery \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --to-revisions=PREVIOUS_RECOVERY_REVISION=100
```

This restores that revision's historical configuration as well as its image.
To keep the **current** configuration and restore only the image, or to reconcile
the latest revision after a traffic rollback before the next CI run, use:

```bash
gcloud run services update SERVICE_NAME \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --image='asia-south1-docker.pkg.dev/decisionguard-ai-builder-cup/decisionguard/backend@sha256:PREVIOUS_DIGEST'
# Verify latest ready revision/digest before moving traffic:
gcloud run services describe SERVICE_NAME \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --format='value(status.latestReadyRevisionName)'
gcloud run revisions describe READY_REVISION \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --format='value(status.imageDigest)'
gcloud run services update-traffic SERVICE_NAME \
  --project=decisionguard-ai-builder-cup --region=asia-south1 --to-latest
```

Rollback selected job templates separately; this does not stop or change executions
that are already running:

```bash
gcloud run jobs update decisionguard-repository-ingestion \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --image='asia-south1-docker.pkg.dev/decisionguard-ai-builder-cup/decisionguard/backend@sha256:PREVIOUS_REPOSITORY_JOB_DIGEST'
gcloud run jobs update decisionguard-adr-ingestion \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --image='asia-south1-docker.pkg.dev/decisionguard-ai-builder-cup/decisionguard/backend@sha256:PREVIOUS_ADR_JOB_DIGEST'
gcloud run jobs describe JOB_NAME \
  --project=decisionguard-ai-builder-cup --region=asia-south1 \
  --format='value(spec.template.spec.template.spec.containers[0].image)'
curl --fail --show-error 'https://API_CLOUD_RUN_URL/health'
```

Verify service traffic points 100% at the intended revision and its
`status.imageDigest` matches the rollback digest. Check health JSON has `status=ok`.
Health is a liveness check, not an evaluation/ingestion smoke run, and does not
spend evaluation budget. Rollback does not undo database changes.

## Local validation (no GCP access)

```bash
python -m unittest discover -s scripts/deployment -p 'test_*.py' -v
actionlint .github/workflows/deploy-backend.yml
cd backend
uv sync --locked
PYTHON_DOTENV_DISABLED=1 JWT_SECRET=decisionguard-ci-only-signing-key-not-for-production \
  uv run --no-sync python -m pytest ai_services/tests \
  -m 'not integration' --ignore=ai_services/tests/test_neo4j.py \
  --ignore=ai_services/tests/test_gemini.py -q
```

Safety tests mock every gcloud/health operation and exercise sequencing, digest
verification, configuration preservation, failure stops, branch restriction,
optional-target preflight and immutable image requirements. They deploy nothing.

Review validation on 2026-10-17 (Windows, Python 3.13): the full offline backend
suite passed with **1,157 passed, 24 skipped, 1 deselected, zero failures**. All
**18 deployment/profile safety tests** passed. Actionlint 1.7.7 (without ShellCheck),
Python syntax, Git whitespace checks, and parsing the operator PowerShell block
also passed. A read-only contract check against the regression integration source
accepted the canonical profile with a custom read limit and rejected absent,
incomplete and malformed profiles. Container/gcloud operations in safety tests
are mocked; actual validation of the combined release image and staged production
revision remains a release prerequisite, not a deployment performed here.

The previous three concurrency-test failures were incomplete settings fixtures:
`GitHubAppService` constructs `AuthStateStore` with `settings.encryption_key`, but
one test supplied an unrestricted mock and two supplied only `app_id`. The shared
`github_settings` fixture now supplies a generated Fernet key to both the store's
environment and each service instance. No runtime contract, concurrency assertion,
ownership check, or test selection was weakened. These tests remain in the full
offline CI gate. Revalidate the final combined production commit after integrating
the separate PR evaluation fix. No workflow, Docker push, Cloud Run update or
production infrastructure change was executed during implementation.

References: [GitHub manual dispatch requirements](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
[Google WIF for deployment pipelines](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines),
[Google auth action](https://github.com/google-github-actions/auth),
[Cloud Run service updates](https://docs.cloud.google.com/sdk/gcloud/reference/run/services/update),
and [Cloud Run job updates](https://docs.cloud.google.com/sdk/gcloud/reference/run/jobs/update).
