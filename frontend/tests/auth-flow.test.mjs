import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, flow, mapper, authService, apiClient, errors;
const storage = new Map();
const location = {
  href: "",
  assign: (url) => {
    location.href = url;
  },
};
const status = {
  user: { id: "usr_1", username: "alice", email: null, avatar_url: null },
  installations: [],
  installation_onboarding_required: false,
  reconnect_required: false,
};

before(async () => {
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  globalThis.window = { location };
  server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  flow = await server.ssrLoadModule("/src/lib/auth-flow.ts");
  mapper = await server.ssrLoadModule("/src/lib/repository-mapper.ts");
  ({ authService } = await server.ssrLoadModule("/src/api/auth-service.ts"));
  ({ apiClient } = await server.ssrLoadModule("/src/lib/api-client.ts"));
  errors = await server.ssrLoadModule("/src/lib/api-error.ts");
});

after(async () => {
  await server?.close();
  delete globalThis.window;
  delete globalThis.localStorage;
});

test("minimal sub/exp token is accepted without profile claims", () => {
  const token = `e30.${Buffer.from(JSON.stringify({ sub: "usr_1", exp: Date.now() / 1000 + 60 })).toString("base64url")}.signature`;
  assert.equal(flow.isValidToken(token), true);
  assert.deepEqual(flow.mapAuthUser(status), {
    id: "usr_1",
    username: "alice",
    email: null,
    avatarUrl: undefined,
  });
});

test("malformed, expired, and subject-less tokens are rejected", () => {
  const token = (payload) =>
    `e30.${Buffer.from(JSON.stringify(payload)).toString("base64url")}.signature`;
  for (const value of [
    "invalid",
    token({ sub: "usr_1", exp: 1 }),
    token({ exp: Date.now() / 1000 + 60 }),
  ]) {
    assert.equal(flow.isValidToken(value), false);
  }
});

test("current status sends returning users to projects, missing grants or reconnect to install", () => {
  assert.equal(flow.getAuthDestination(status), "/projects");
  assert.equal(
    flow.getAuthDestination({
      ...status,
      installation_onboarding_required: true,
    }),
    "/install"
  );
  assert.equal(
    flow.getAuthDestination({ ...status, reconnect_required: true }),
    "/install"
  );
});

test("callback routes are allowlisted and backend dashboard maps to projects", () => {
  assert.equal(flow.getCallbackDestination("/dashboard"), "/projects");
  assert.equal(flow.getCallbackDestination("/install"), "/install");
  assert.equal(
    flow.getCallbackDestination("https://malicious.example"),
    "/projects"
  );
  assert.equal(flow.getCallbackDestination(null), "/projects");
});

test("repository mapping preserves installation and distinct GitHub/internal IDs", () => {
  const dto = {
    id: "repo_101",
    github_repository_id: "101",
    installation_id: "10",
    full_name: "owner/repo",
    name: "repo",
    description: null,
    is_private: false,
    repository_url: "https://github.com/owner/repo",
    default_branch: "main",
    tracked_branch: "develop",
    status: "COMPLETED",
    tracked_repository_id: "repo_101",
    indexed_commit_sha: "abc",
    updated_at: null,
  };
  const repo = mapper.mapRepository(dto);
  assert.equal(repo.id, "101");
  assert.equal(repo.trackedRepositoryId, "repo_101");
  assert.equal(repo.installationId, "10");
  assert.equal(repo.fullName, "owner/repo");
  assert.equal(repo.trackedBranch, "develop");
  assert.equal(repo.updatedAt, null);
  assert.equal(repo.indexedCommitSha, "abc");
  const unindexed = mapper.mapRepository({
    ...dto,
    id: null,
    tracked_repository_id: null,
    status: "NOT_INDEXED",
  });
  assert.equal(unindexed.trackedRepositoryId, null);
  assert.equal(unindexed.status, "NOT_INDEXED");
});

test("installation start uses bearer auth and temporary-cookie credentials; me supplies profile", async () => {
  storage.set("token", "application-token");
  const calls = [];
  apiClient.defaults.adapter = async (config) => {
    calls.push(config);
    return {
      data:
        config.url === "/auth/me"
          ? status
          : {
              installation_url:
                "https://github.com/apps/test/installations/new?state=123",
            },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
  };
  assert.deepEqual(await authService.getStatus(), status);
  assert.equal(
    await authService.startInstallation(),
    "https://github.com/apps/test/installations/new?state=123"
  );
  assert.equal(calls[0].headers.Authorization, "Bearer application-token");
  assert.equal(calls[1].method, "post");
  assert.equal(calls[1].url, "/auth/github/install");
  assert.equal(calls[1].withCredentials, true);
  assert.equal(calls[1].headers.Authorization, "Bearer application-token");
  assert.notEqual(calls[0].withCredentials, true);
});

test("access-denied and network errors stay distinct from missing installation", () => {
  const denied = {
    isAxiosError: true,
    response: { status: 403, data: { detail: "Denied" } },
  };
  assert.equal(errors.isAccessDenied(denied), true);
  assert.match(errors.getApiErrorMessage(denied, "Retry"), /installation/);
  assert.equal(
    errors.getApiErrorMessage(new Error("network"), "Retry"),
    "Retry"
  );
});

test("a stale 401 cannot log out a newly authorized token", async () => {
  storage.set("token", "old-token");
  location.href = "";
  apiClient.defaults.adapter = async (config) => {
    storage.set("token", "new-token");
    throw { isAxiosError: true, config, response: { status: 401 } };
  };
  await assert.rejects(authService.getStatus());
  assert.equal(storage.get("token"), "new-token");
  assert.equal(location.href, "");
});

test("current-token 401 clears application auth and restarts login", async () => {
  storage.set("token", "current-token");
  apiClient.defaults.adapter = async (config) => {
    throw { isAxiosError: true, config, response: { status: 401 } };
  };
  await assert.rejects(authService.getStatus());
  assert.equal(storage.get("token"), undefined);
  assert.equal(location.href, "/auth/login");
});
