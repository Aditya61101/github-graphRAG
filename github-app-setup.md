# GitHub App migration: prototype setup

## Backend behavior

One GitHub App now supplies OAuth identity, installation inventory, and
installation-authenticated indexing. Public repository grants are verified through
the current user's `/user/installations` and
`/user/installations/{id}/repositories` results intersected with freshly reconciled
App installation inventory. A readable public `/repos/...` endpoint is not a grant.

Both existing private-repository checks remain. Numeric GitHub IDs determine
identity; matching usernames never link different identities. Existing matching
accounts retain application user IDs. GitHub tokens are Fernet-encrypted at rest;
expired/legacy/revoked user connections require login again, with no automatic
refresh. Application bearer JWT claims contain only `sub` and `exp`.

Installation tokens are cached by installation, repository scope, and permissions,
expire 60 seconds early, and are invalidated on inventory/status/selection changes.
Indexing receives repository/installation IDs, resolves installation credentials
when work executes, and checks access again before graph publication and success.
User tokens are never an automated indexing fallback.

Signed lifecycle webhooks reconcile full current state rather than applying stale
selection deltas. Suspension, deletion, and removal disable affected access without
deleting repository/ADR data. Installation creation does not assign ownership from
webhook sender. Revocation disables the user's connection, not the installation;
late revocation events cannot revoke a newly valid reauthorization token.

All repository HTTP operations share current ownership/App/user authorization.
Conversations bind to the authenticated user and repository; checkpoint keys
include both. A conversation ID cannot be reused across repositories by the same
user. Different users' IDs have separate histories. Tool arguments cannot override
the backend repository scope.

## GitHub App registration

Create a **GitHub App** under GitHub developer settings, not an OAuth App.

- App name/slug: choose your DecisionGuard prototype name.
- Homepage: your frontend URL.
- User authorization callback: `https://YOUR_BACKEND/auth/github/callback`.
  For local development, `http://localhost:8000/auth/github/callback` is supported.
- Enable **Request user authorization (OAuth) during installation**. Keep the
  configured callback as the first callback URL. A separate setup URL is not used.
- Keep **Expire user authorization tokens** enabled. Refresh-token support is
  intentionally not implemented.
- Webhook URL: `https://YOUR_BACKEND/webhooks/github` (use a HTTPS tunnel locally).
- Set a random webhook secret matching the environment.
- Repository permissions: **Metadata read**, **Contents read**, **Pull requests read**.
  No write permissions, user-email OAuth scope, or organization membership tables.
- Subscribe to **Push** and **Pull request**. Installation/installation-repository
  and App authorization lifecycle events are also handled.
- Install on the desired account and select repositories. The backend ignores
  private repositories even if they were selected.
- Generate an RSA private key; store it outside version control, e.g.
  `backend/secrets/github-app.pem` (already ignored). Do not commit PEM material.

Official references: [App user authorization and PKCE](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app),
[installation APIs](https://docs.github.com/en/rest/apps/installations),
[App APIs](https://docs.github.com/en/rest/apps/apps).

## Environment and dependencies

See `backend/.env.github-app.example`. Required settings:

| Variable | Purpose |
|---|---|
| `GITHUB_APP_ID` | Numeric App ID (not installation ID) |
| `GITHUB_APP_SLUG` | Installation URL slug |
| `GITHUB_APP_CLIENT_ID` / `GITHUB_APP_CLIENT_SECRET` | App OAuth credentials |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Readable unencrypted RSA PEM location |
| `GITHUB_APP_WEBHOOK_SECRET` | Raw-body HMAC secret |
| `GITHUB_APP_CALLBACK_URL` | Fixed authorization callback URL |
| `FRONTEND_URL` | Fixed frontend redirect origin |
| `OAUTH_SESSION_SECRET` | Independent signing secret, at least 32 characters |
| `GITHUB_TOKEN_ENCRYPTION_KEY` | Independent Fernet key for stored GitHub tokens |
| `JWT_SECRET` | Existing application bearer JWT signing secret |

The old `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET` OAuth settings are no longer used.
Paths are relative to the backend working directory. URLs require HTTPS outside
localhost. OAuth session cookies are short-lived, HttpOnly, SameSite=Lax, and
Secure when the configured callback uses HTTPS. They hold temporary OAuth state,
not application JWTs. Forwarded host headers never determine callbacks.

From `backend`, generate independent secrets locally:

```powershell
uv sync
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use separate random values for session/JWT/webhook secrets. Do not share generated
values in logs or chat. `pyjwt[crypto]` and `cryptography` are explicit dependencies;
no unrelated dependency upgrades are required.

## Existing SQLite database upgrade

Stop the backend and back up the existing SQLite database (and WAL files, if any).
Configure the encryption key first. Then, from `backend`:

```powershell
uv run python -m ai_services.ingestion.persistence.migrate_github_app
```

The explicit version-1 upgrade adds installation/inventory/conversation tables and
connection/repository access fields, encrypts legacy tokens, preserves IDs and ADR
foreign keys, and marks legacy connections/unmapped repositories reconnect-required.
Known ownership is preserved or recovered from the existing connection relationship.
The upgrade is idempotent. It propagates errors; startup rejects an unversioned old
database instead of pretending `create_all()` added columns. A fresh empty database
is created at version 1. Keep the encryption key stable; losing it requires reconnect.

Legacy users log in through the App again, then select/re-ingest their granted
repository to establish its verified installation binding. Neo4j repository IDs,
existing ADR associations, and stored ingestion state do not change. No live
database upgrade or GitHub registration changes are made by this implementation.

## Frontend-facing contracts

1. Navigate to `GET /auth/github/login`. PKCE and state protect explicit login.
2. Callback redirects to `/auth/success?token=...&next=/dashboard` or `next=/install`.
   Continue existing bearer-token handling; use `next` for routing.
3. `GET /auth/me` with bearer JWT returns `user`, installation summaries,
   `installation_onboarding_required`, and `reconnect_required`; never credentials.
4. To install, make authenticated `POST /auth/github/install`, **with browser
   credentials included** so the temporary session cookie is retained. It returns
   `installation_url`; navigate there. The callback verifies the initiating account
   and current installation. Missing local transactions start protected login again.
5. `GET /repositories` returns currently granted public repositories with
   `installation_id` and local status (including `NOT_INDEXED`). Stale tracked
   repositories are not appended. `GET /repositories/tracked` filters current access.
6. `POST /repositories/ingest`: existing repository/branch/force_full/sync fields;
   optional `installation_id` is a verified selection hint, never token authority.
   Legacy `connection_id` hints remain ignored, not credential selectors.
7. Detail/sync/query/graph/ADR endpoints require current App grants and ownership.
   `/query` requires `repository_id`. A conflicting conversation scope returns 409.
   Bearer JWT auth remains unchanged; no application JWT cookies/refresh endpoint.
8. ADR upload remains the `files`-only one-or-many contract from `adr-batch-upload.md`.

Use access/onboarding state, not a first-login flag. Prior installations work even
before the user's first DecisionGuard login. Cancelled/inaccessible/pending
installations are never activated from callback hints; the user can retry onboarding.

## Prototype limitations

- The existing JWT-in-redirect-query delivery is retained. URLs may leak through
  browser history/access logs/referrers; frontend should consume then remove the
  token from the URL, and proxies must not log callback/success query strings.
- Single FastAPI worker only. OAuth transactions, installation-token cache,
  per-repository locks, BackgroundTasks, and conversational checkpoints are local
  to a process. Restart loses pending work/state; there is no durable queue or
  distributed locking. Restarted OAuth flows must begin again.
- User tokens expire and require reconnect; no automatic refresh is implemented.
- One application user owns each tracked repository, including organization repos.
  Another GitHub-authorized user can discover it but receives ownership conflict
  when trying to connect/index it. No workspaces or ownership transfers.
- GitHub list/inventory verification is live and can be expensive on large installs.
  Rate-limit/network failures fail closed. Long indexing may need retry if an
  installation token expires during Git operations; credentials aren't silently reused.
- Authorization is rechecked at publication boundaries, not a transaction shared
  by GitHub, SQLite, and Neo4j. Revocation can leave already-written graph data;
  affected HTTP access is blocked. Existing graph data is retained, not garbage-collected.
- No private repositories, PR analysis, deployment, or live GitHub setting changes.
- Tests use mocked GitHub HTTP, isolated SQLite/FastAPI, and existing graph fixtures.
  Real GitHub App registration/authorization/webhook round trips require manual
  verification after the operator configures the App and upgrades local SQLite.

## Implementation and verification report

No applicable `AGENTS.md` was found in the workspace or checked parent directories.
The previous path used traditional OAuth `/user/repos` discovery and stored user
credentials for indexing. It also allowed query calls without repository scope and
used a raw conversation ID for checkpoints. These paths now use the App access
boundary described above. RKG algorithms, ADR ingestion semantics, and retrieval
providers were not redesigned.

### Changed files

Paths below are relative to `backend/`, except this setup guide:

- New App module: `ai_services/github_app/__init__.py`, `settings.py`, `service.py`.
- Persistence: `ai_services/ingestion/persistence/models.py`, `database.py`,
  `sqlite_store.py`, new `migrate_github_app.py`.
- Indexing authorization/publication: `ai_services/ingestion/service.py`,
  `sources/github.py`, `rkg/ingestion_pipeline.py`,
  `rkg/builders/repo_ingestion_pipeline.py`.
- Shared API authorization: new `api_services/app/utils/github_app_access.py`;
  routers `auth.py`, `repositories.py`, `query.py`, `webhook.py`;
  `utils/jwt_utils.py`, `models/repository.py`, `config.py`, `main.py`.
- Scope enforcement: `ai_services/graph/service.py`, `ingestion/adr/service.py`,
  `agents/rag_agent/agent.py`, `agents/rag_agent/tools/query_tool.py`.
- Tests: new `ai_services/tests/test_github_app.py`; updated `test_adr_upload.py`,
  `test_repository_graph.py`, `test_query_graph_context.py`,
  `test_reranker_lifecycle.py`.
- Dependencies/configuration: `pyproject.toml`, `uv.lock`,
  `.env.github-app.example`; root `github-app-setup.md`.

The task input `github_app_migration.md` was not changed. Dependencies add explicit
Cryptography and the PyJWT RSA extra; the lock update does not upgrade unrelated
packages. Git credentials use transient subprocess environment configuration,
not command arguments, and reset inherited helpers/authorization headers.
Permission lifecycle events invalidate tokens even when inventory is unchanged.

### Exact final commands and results

Run from `backend/`:

```powershell
uv lock
uv run --no-sync pytest ai_services/tests/test_github_app.py ai_services/tests/test_adr_upload.py ai_services/tests/test_adr_neo4j_pipeline.py ai_services/tests/test_repository_graph.py ai_services/tests/test_query_graph_context.py ai_services/tests/test_ingestion_coverage.py ai_services/tests/test_reranker_lifecycle.py -q
uv run --no-sync pytest ai_services/tests --ignore=ai_services/tests/test_neo4j.py --ignore=ai_services/tests/test_gemini.py -q
uv run --no-sync python -m compileall -q ai_services/github_app ai_services/ingestion api_services/app ai_services/agents/rag_agent
```

- Dependency resolution: successful, 176 packages resolved.
- App/affected regression suite: **143 passed, 1 skipped**, 19.99 seconds.
- Complete offline backend suite: **171 passed, 1 skipped, 1 failed**, 21.41 seconds.
  The unchanged failure is
  `test_canonicalization.py::test_invalid_entity_names_are_rejected_and_writer_preserves_reserved_identity_fields`:
  its valid-entity fixture has no embedding, but the existing writer requires one.
  The fixture and writer were left unchanged as unrelated to this migration.
- Compilation: successful.
- Root command `git -c core.whitespace=cr-at-eol diff --check`: successful.

The skip is the existing Windows symlink-permission case. The 124 warnings are
from the existing short application JWT secret in the test environment; use an
independent random secret of at least 32 bytes in configuration. Live Neo4j and
Gemini smoke scripts were excluded deliberately. No real GitHub calls, deployment,
live installation changes, or live database migration were performed.
