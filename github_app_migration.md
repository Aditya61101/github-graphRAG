Implement a minimal but complete migration of DecisionGuard’s FastAPI backend from its current GitHub OAuth integration to a single GitHub App.

This is a public-repository-only prototype. Implement the changes, add focused tests, and update configuration documentation. Inspect the current code before editing and follow applicable AGENTS.md instructions.

## Product flow

Use the same GitHub App for:
- User OAuth identity.
- Repository installation/access.
- Installation-authenticated background indexing.

The desired UI flow is:

1. User logs into DecisionGuard through the GitHub App’s OAuth authorization flow.
2. Backend verifies the GitHub identity and issues DecisionGuard’s own bearer JWT.
3. Backend discovers the user’s accessible App installations and repositories.
4. If no usable installation exists, frontend shows “Install DecisionGuard.”
5. Clicking install redirects to GitHub’s installation screen, where the user selects repositories.
6. After a verified installation return, redirect to the dashboard.
7. Dashboard lists granted public repositories accessible to the current user, including repositories not yet indexed.
8. Returning users with a usable installation go directly to the dashboard.

Use current installation/access state, not a permanent “first login” flag. Support installations created before the user’s first DecisionGuard login.

## Scope constraints

- Keep public repositories only. Preserve both existing private-repository rejection checks.
- Keep DecisionGuard’s Authorization: Bearer JWT authentication.
- JWT identity claims should contain only the application user ID as `sub`, plus expiration.
- Do not introduce cookie-based application JWT authentication, application refresh tokens, or a new application session architecture.
- Temporary OAuth state/session cookies are acceptable. Replace the hard-coded session signing secret.
- Do not expand this task into a JWT delivery redesign. Document the existing redirect-token delivery limitation if retained.
- Keep SQLite, existing repository IDs, Neo4j repository identifiers, ADR relationships, and the current single-user repository ownership policy.
- Do not add workspaces, organization membership tables, shared repository ownership, a broker, durable queues, or distributed locking.
- Keep BackgroundTasks for the single-worker prototype; document restart-related job loss.
- Do not implement private-repository support or PR analysis features.

## Inspect these existing areas

Paths below are relative to backend/; adapt to the actual checkout:

- api_services/app/routers/auth.py
- api_services/app/utils/jwt_utils.py
- api_services/app/routers/repositories.py
- api_services/app/routers/webhook.py
- api_services/app/routers/query.py
- api_services/app/main.py
- api_services/app/config.py
- api_services/app/models/repository.py
- ai_services/ingestion/persistence/models.py
- ai_services/ingestion/persistence/database.py
- ai_services/ingestion/persistence/sqlite_store.py
- ai_services/ingestion/sources/credentials.py
- ai_services/ingestion/sources/github.py
- ai_services/ingestion/service.py
- ai_services/graph/service.py
- ai_services/ingestion/adr/service.py
- ai_services/agents/rag_agent/agent.py
- ai_services/agents/rag_agent/tools/query_tool.py
- Relevant existing tests and dependency files.

## Required implementation

### 1. GitHub App configuration

Add clearly named settings for App ID, slug, client ID, client secret, private-key location, webhook secret, fixed callback URL, frontend URL, and OAuth session signing secret.

Validate required configuration and key material with useful errors. Never log secrets. Use a configured callback URL rather than trusting arbitrary forwarded host headers.

Add necessary RSA-signing dependencies explicitly. Provide a secret-free environment example and setup instructions.

### 2. User authorization and onboarding

Retain Authlib where practical, but use GitHub App client credentials.

Remove the traditional `user:email` OAuth scope. Request App permissions through registration settings.

Preserve OAuth state validation and use PKCE for the explicit login flow where supported. Validate GitHub HTTP responses before accessing identity fields.

Upsert users only by stable numeric GitHub identity. Fix the current username-based fallback that can link an unfamiliar GitHub identity to an existing application user. Preserve existing application user IDs for matching GitHub accounts.

Add an authenticated onboarding/status endpoint, such as GET /auth/me, returning:
- User identity.
- Whether installation onboarding is required.
- Accessible installation summaries.
- Any reconnect-required state.

Do not return GitHub credentials.

Add an installation-start endpoint. Bind its state to the initiating user/browser using a short-lived transaction. Since browser navigation does not automatically carry a bearer header, support starting installation through an authenticated request that returns the GitHub installation URL.

Configure/document “Request user authorization during installation.” Distinguish normal login and installation completion so callbacks return to the correct UI destination.

Never trust callback installation_id or setup_action as authorization evidence. Verify installations through GitHub using the authenticated user and confirm they belong to this App.

If a callback arrives without a valid local transaction—for example, installation started directly from GitHub—restart a normal protected OAuth login before completing onboarding. Do not disable state validation.

Handle cancellation, authorization failure, inaccessible installations, and pending organization approval without marking them as active.

### 3. Minimal persistence changes

Add a GitHub installation model containing:
- Installation ID.
- App ID.
- Stable account ID, account login and account type.
- Active/suspended/deleted status.
- Repository selection mode.
- Relevant timestamps.

Bind tracked repositories to a verified installation and add access state separately from ingestion status.

Persist enough installation repository inventory to identify granted/removed repositories, including repositories not yet tracked. Choose the simplest schema that supports correct reconciliation.

Update GitHub user connections to distinguish App credentials from legacy OAuth credentials and record access-token expiration. Encrypt persisted GitHub tokens using a separately configured key.

For this prototype, automatic user-token refresh is out of scope. Keep token expiration enabled; require reconnect/login when credentials expire or become invalid. Do not silently use expired tokens or substitute installation tokens for user access verification.

Provide an explicit, versioned upgrade for existing SQLite databases. Do not rely on create_all() to add columns or swallow migration errors. Preserve existing data and mark unmapped repositories as reconnect-required.

### 4. Small GitHub App service

Implement a reusable backend service for:
- Listing user-accessible App installations.
- Listing repositories accessible to the user within an installation.
- Reading/reconciling installation repository inventory.
- Generating an RS256 GitHub App JWT.
- Minting installation tokens.
- Caching installation tokens until shortly before their returned expiration.

Paginate GitHub list endpoints. Installation tokens must be cached by installation and requested scope, and invalidated when access changes. Treat tokens as opaque variable-length values.

Narrow indexing tokens to the required repository and permissions where practical.

### 5. Repository discovery and authorization

Replace /user/repos discovery with the GitHub App installation flow:
- GET /user/installations.
- GET /user/installations/{installation_id}/repositories.

Dashboard responses must list currently granted public repositories the user can access and merge in local ingestion status.

Do not append stale tracked repositories as though they remain accessible. Return visibility truthfully.

Keep current single-user ownership checks for tracked repository operations. Do not silently transfer ownership when another authorized GitHub user selects the same repository; preserve/document that prototype limitation.

Use one shared authorization helper for repository operations. It must verify:
- Authenticated DecisionGuard user.
- Existing ownership where the operation concerns a tracked repository.
- Active installation.
- Repository currently granted to that installation.
- Current GitHub user access where needed.

A public repository being readable through GitHub’s general API does not prove it was granted to this App. Never authorize ingest merely because GET /repos/{owner}/{repo} succeeds.

Apply this policy to ingest, sync, tracked listings, detail, query, graph, and ADR endpoints.

Any client-supplied installation ID is a selection hint that must be verified, not permission to retrieve that installation’s token.

### 6. Installation-authenticated indexing

Replace stored user-token resolution for clone/fetch/indexing and webhook synchronization with verified installation-token resolution.

Pass repository/installation identifiers to background work and acquire credentials when the work executes. Recheck access before execution and before publishing indexed results.

Remove user-token fallback from automated indexing. Do not pass legacy OAuth credentials after an App access denial.

Retain GitHubRepositorySource and its existing HTTPS Git mechanism where practical. Keep credentials out of repository URLs, persistent Git config, API responses, and logs.

Handle expired credentials and access-denied errors without misclassifying them as corrupted clones.

### 7. Webhook lifecycle

Retain raw-body HMAC-SHA256 verification and fail-closed behavior.

Handle:
- installation.created.
- installation.deleted.
- installation.suspend and unsuspend.
- installation.new_permissions_accepted.
- installation_repositories.added and removed.
- github_app_authorization.revoked.
- Existing push and pull_request behavior.

Installation creation must not assign ownership based solely on webhook sender.

Reconcile full repository inventory after selection changes, including switching from all repositories to selected repositories.

Deletion, suspension, and repository removal must disable affected operations and invalidate installation-token caches. User authorization revocation must disable that user’s GitHub connection without deleting the installation.

Resolve push events by verified installation ID and numeric repository ID. Keep tracked-branch filtering and public-only behavior.

Handle ping independently of ingestion-service availability. Validate JSON object shape and return sanitized errors.

Make handlers safe for duplicate delivery and reconcile current GitHub state so late events do not reactivate removed access. Do not introduce a durable queue in this task.

### 8. Small authorization fixes required by this migration

Require an authorized repository ID for /query; do not allow unrestricted retrieval when it is omitted.

Bind conversation history to the authenticated user and repository so two users cannot share history by submitting the same conversation_id. Prevent reuse across repository scopes.

Ensure model-supplied tool arguments cannot override the backend-authorized repository.

## Verification

Add focused tests using mocked GitHub HTTP responses and isolated FastAPI apps; auth/webhook tests should not load LLMs or require live Neo4j.

Cover:
- Existing GitHub identity retains its application user ID.
- Different GitHub numeric IDs never merge through matching usernames.
- Valid/invalid/missing/replayed callback state.
- Installation completion and direct-GitHub onboarding.
- Existing installation skips installation onboarding.
- Installation hints cannot select another user’s installation.
- Pagination and public-only filtering.
- Unselected public repository cannot be ingested.
- Installation credentials are used for manual and webhook indexing.
- Token expiration and cache invalidation.
- Installation removal/suspension and repository removal block affected access.
- Callback-before-webhook and webhook-before-callback.
- Invalid webhook signatures and malformed payloads.
- Missing query scope and cross-user/cross-repository conversation isolation.
- Existing SQLite upgrade preserves IDs and relationships.

Run relevant existing and new tests. Ensure pytest configuration discovers any added API tests.

## Deliverables

Implement the migration rather than stopping at a design report.

At completion, summarize:
- Changed files and behavior.
- New/changed frontend-facing endpoint contracts.
- Exact GitHub App settings, permissions, callback/webhook URLs, and environment variables required.
- Database upgrade command and legacy-user reconnect behavior.
- Tests run and results.
- Remaining prototype limitations.

Do not change live GitHub settings, deploy, expose secrets, or add unrelated production infrastructure.