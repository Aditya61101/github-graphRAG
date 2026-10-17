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
   service account. The action mints a masked, one-hour account access token used
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

Only `gcloud run services update --image`, `services update-traffic --to-latest`,
and `jobs update --image` mutate Cloud Run. No environment, secrets, Cloud SQL,
runtime identity, resources, concurrency, HTTP/task timeout, command/args, budgets,
queue, migration or IAM flags are passed. The helper also compares the runtime
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
- Add these environment variables (Settings → Environments → backend-production):

  | Variable | Value |
  | --- | --- |
  | `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-backend/providers/github` |
  | `GCP_DEPLOY_SERVICE_ACCOUNT` | `github-backend-deployer@decisionguard-ai-builder-cup.iam.gserviceaccount.com` |

- No backend GitHub secrets are required. Do not copy runtime secrets or GCP keys
  into GitHub. Leave the Firebase service-account secret and frontend settings alone.
- GitHub requires a `workflow_dispatch` workflow to exist on the repository's
  **default branch** for it to be discoverable/dispatchable. Review and land the
  same workflow, helper, tests and documentation on the default branch and on
  `integration/production`. Do not change the default branch just for deployment.
  Dispatch using the production ref; a default-branch dispatch fails the guard
  unless the default branch itself is `integration/production`.

## Workload Identity Federation setup (administrator, once)

These are setup instructions, not commands run by the workflow. Review them with
the GCP administrator. Do not run them as part of implementation or a deployment.
Use existing approved pool/provider/account resources where appropriate.

Required APIs: IAM, IAM Service Account Credentials, Security Token Service,
Cloud Resource Manager, Artifact Registry and Cloud Run. No Cloud Build API or
Cloud Build account is required; Docker builds on the GitHub runner.

Example in Bash/Cloud Shell; replace the GitHub identifiers with the actual values.
Use numeric repository and owner IDs from GitHub to prevent name-reuse attacks.
Project **number** is distinct from project ID.

```bash
PROJECT=decisionguard-ai-builder-cup
PROJECT_NUMBER=REPLACE_WITH_GCP_PROJECT_NUMBER
OWNER_REPO=REPLACE_OWNER/REPLACE_REPOSITORY
REPOSITORY_ID=REPLACE_WITH_NUMERIC_REPOSITORY_ID
OWNER_ID=REPLACE_WITH_NUMERIC_OWNER_ID
DEPLOYER="github-backend-deployer@$PROJECT.iam.gserviceaccount.com"

gcloud iam service-accounts create github-backend-deployer --project="$PROJECT"
gcloud iam workload-identity-pools create github-backend \
  --project="$PROJECT" --location=global --display-name='GitHub backend production'
gcloud iam workload-identity-pools providers create-oidc github \
  --project="$PROJECT" --location=global --workload-identity-pool=github-backend \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping='google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id' \
  --attribute-condition="assertion.repository_id == '$REPOSITORY_ID' && assertion.repository_owner_id == '$OWNER_ID' && assertion.ref == 'refs/heads/integration/production' && assertion.event_name == 'workflow_dispatch' && assertion.environment == 'backend-production' && assertion.workflow_ref == '$OWNER_REPO/.github/workflows/deploy-backend.yml@refs/heads/integration/production'"

gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" \
  --project="$PROJECT" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github-backend/attribute.repository_id/$REPOSITORY_ID"
```

Use a dedicated pool/provider so another provider with a weaker condition cannot
reuse this principal set. Keep the provider's default audience; the auth action
uses its full provider resource name. Wait for IAM propagation before the first
manual run. Do not add broad owner/editor or service-account key permissions.

## Deployment account permissions

Administrators provision these grants separately; the workflow never changes IAM.

| Principal | Grant and scope | Purpose |
| --- | --- | --- |
| Federated repository principal set above | `roles/iam.workloadIdentityUser` on deployment account | Exchange OIDC credentials for account impersonation |
| Deployment account | `roles/artifactregistry.writer` on repository `decisionguard` in `asia-south1` | Push/read image and resolve digest |
| Deployment account | `roles/run.developer` on each of the three existing services and two jobs | Read/update resources and read revisions; no IAM policy changes |
| Deployment account | `roles/iam.serviceAccountUser` on each existing runtime service account used by selected targets | Act as the existing runtime account during an image update |
| Deployment account | `roles/serviceusage.serviceUsageConsumer` on project, if required for quota project usage | Consume enabled APIs |

For example (administrator only), grant Artifact Registry writer with:

```bash
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

## Integration and manual release

1. Review the `feature/backend-cicd` commit. Integrate the concurrent PR evaluation
   regression fix independently; this change does not contain that fix.
2. In the normal integration checkout, bring the reviewed CI/CD commit onto
   `integration/production` with `git cherry-pick <backend-cicd-commit>` (or the
   team's reviewed PR process). Resolve overlaps only in deployment files. Run the
   offline tests below on the final combined production commit. Push the reviewed
   branch through the normal branch protection process.
3. Land the same deployment files on the repository default branch as described
   above. Configure the GitHub environment, WIF provider and scoped deployer grants.
   None of these integration/setup actions are performed by this implementation.
4. Actions → **Deploy backend to production** → Run workflow → select
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

Implementation validation on 2026-10-17 (Windows, Python 3.13): locked dependency
installation, all 10 deployment safety tests, Python syntax, actionlint 1.7.7
(without ShellCheck), and Git whitespace checks passed. The full offline backend
run completed with **1,154 passed, 24 skipped, 1 deselected and 3 failed**. All three
failures are in the unchanged `ai_services/tests/test_database_concurrency.py`:

- `test_concurrent_conversation_binding_preserves_scope`: the settings mock yields
  a mock `encryption_key`, which Fernet cannot decode.
- `test_concurrent_installation_inventory_and_repository_binding`: the settings
  stub has no `encryption_key`.
- `test_binding_cannot_reactivate_removed_inventory`: the settings stub has no
  `encryption_key`.

These fixture/application-contract issues require a separate change outside this
deployment-only scope. The workflow includes these tests and will block rollout
until they pass. Revalidate the combined production commit after integrating those
fixes and the concurrent PR evaluation fix. No workflow, Docker push, Cloud Run
update or production infrastructure change was executed during implementation.

References: [GitHub manual dispatch requirements](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
[Google WIF for deployment pipelines](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines),
[Google auth action](https://github.com/google-github-actions/auth),
[Cloud Run service updates](https://docs.cloud.google.com/sdk/gcloud/reference/run/services/update),
and [Cloud Run job updates](https://docs.cloud.google.com/sdk/gcloud/reference/run/jobs/update).
