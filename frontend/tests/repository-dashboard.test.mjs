import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

let server, flow, service, client, Card;
const storage = new Map();
before(async () => {
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  globalThis.window = { location: { href: "" } };
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  flow = await server.ssrLoadModule("/src/lib/repository-dashboard.ts");
  ({ repositoryService: service } = await server.ssrLoadModule(
    "/src/api/repository-service.ts"
  ));
  ({ apiClient: client } = await server.ssrLoadModule(
    "/src/lib/api-client.ts"
  ));
  ({ RepositoryCard: Card } = await server.ssrLoadModule(
    "/src/components/repositories/repository-card.tsx"
  ));
});
after(async () => {
  await server?.close();
  delete globalThis.localStorage;
  delete globalThis.window;
});
const repository = {
  id: "101",
  githubRepositoryId: "101",
  trackedRepositoryId: null,
  installationId: "10",
  fullName: "example-org/service",
  name: "service",
  repositoryUrl: "https://github.com/example-org/service",
  defaultBranch: "main",
  trackedBranch: null,
  status: "NOT_INDEXED",
  indexedCommitSha: null,
  description: "Public service",
  isPrivate: false,
  updatedAt: null,
};

function render(props = {}) {
  return renderToStaticMarkup(
    createElement(Card, { repository, pending: false, onIndex() {}, ...props })
  );
}

test("states distinguish accessible repositories, tracked records, and completed indexing", () => {
  assert.equal(flow.getRepositoryState(repository), "available");
  assert.equal(
    flow.getRepositoryState({ ...repository, status: "COMPLETED" }),
    "unknown"
  );
  for (const [status, state] of [
    ["IDLE", "available"],
    ["NOT_INDEXED", "available"],
    ["INDEXING", "indexing"],
    ["COMPLETED", "ready"],
    ["FAILED", "failed"],
    ["NEW_STATE", "unknown"],
  ]) {
    assert.equal(
      flow.getRepositoryState({
        ...repository,
        trackedRepositoryId: "repo_101",
        status,
      }),
      state
    );
  }
});

test("discoverable collaborator repositories never inherit administrative or stored-knowledge actions", () => {
  for (const status of ["NOT_INDEXED", "COMPLETED", "FAILED", "INDEXING"]) {
    const item = { ...repository, status };
    assert.equal(flow.getRepositoryAction(item), null);
    const html = render({ repository: item });
    assert.doesNotMatch(
      html,
      /Enable monitoring|Retry indexing|Reindex|Upload ADR|[/]projects[/]/
    );
    assert.match(html, /View on GitHub/);
  }
  assert.equal(
    flow.getRepositoryAction({
      ...repository,
      trackedRepositoryId: "repo_101",
    }),
    "enable"
  );
  assert.equal(
    flow.getRepositoryAction({
      ...repository,
      trackedRepositoryId: "repo_101",
      status: "FAILED",
    }),
    "retry"
  );
  assert.equal(
    flow.getRepositoryAction({
      ...repository,
      trackedRepositoryId: "repo_101",
      status: "COMPLETED",
    }),
    null
  );
});

test("search matches repository name or owner and status filters use server classifications", () => {
  const items = [
    repository,
    {
      ...repository,
      githubRepositoryId: "102",
      fullName: "alice/frontend",
      name: "frontend",
      trackedRepositoryId: "repo_102",
      status: "COMPLETED",
    },
  ];
  assert.equal(flow.filterRepositories(items, "EXAMPLE-ORG", "all").length, 1);
  assert.equal(flow.filterRepositories(items, " frontend ", "ready").length, 1);
  assert.equal(flow.filterRepositories(items, "example", "ready").length, 0);
  assert.equal(flow.filterRepositories(items, "", "available").length, 1);
  assert.equal(
    flow.uniqueRepositories([...items, { ...repository, installationId: "20" }])
      .length,
    2
  );
});

test("cards expose real branch and state without fabricated statistics or workspace links", () => {
  const html = render({
    repository: {
      ...repository,
      trackedRepositoryId: "repo_101",
      trackedBranch: "develop",
      status: "COMPLETED",
    },
  });
  assert.match(html, /Ready|develop|Public|example-org/);
  assert.doesNotMatch(
    html,
    /Enable monitoring|ADR records|Entities|[/]projects[/]repo_101/
  );
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(render({ error: "Access denied" }), /role="alert"/);
});

test("sync uses the existing authorized asynchronous endpoint and does not index untracked repositories", async () => {
  storage.set("token", "application-token");
  const calls = [];
  client.defaults.adapter = async (config) => {
    calls.push(config);
    return {
      data: { status: "accepted" },
      status: 202,
      statusText: "Accepted",
      headers: {},
      config,
    };
  };
  await assert.rejects(
    service.syncRepository(repository),
    /tracked repository/
  );
  assert.equal(calls.length, 0);
  await service.syncRepository({
    ...repository,
    trackedRepositoryId: "repo_101",
  });
  assert.equal(calls[0].url, "/repositories/example-org/service/sync");
  assert.equal(calls[0].method, "post");
  assert.deepEqual(JSON.parse(calls[0].data), {
    force_full: false,
    sync: false,
  });
  assert.equal(calls[0].headers.Authorization, "Bearer application-token");
  client.defaults.adapter = async (config) => ({
    data: { status: "failed" },
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  });
  await assert.rejects(
    service.syncRepository({ ...repository, trackedRepositoryId: "repo_101" }),
    /did not accept/
  );
});

test("GitHub-specific repository 401 preserves the app session; invalid app JWT still signs out", async () => {
  for (const [detail, preserved] of [
    ["GitHub reconnect required", true],
    ["Could not validate credentials", false],
  ]) {
    storage.set("token", "application-token");
    window.location.href = "";
    client.defaults.adapter = async (config) => {
      throw {
        isAxiosError: true,
        config,
        response: { status: 401, data: { detail } },
      };
    };
    await assert.rejects(service.getRepositories());
    assert.equal(storage.has("token"), preserved);
    assert.equal(window.location.href, preserved ? "" : "/auth/login");
    assert.equal(
      flow.needsGitHubReconnect({
        isAxiosError: true,
        response: { status: 401, data: { detail } },
      }),
      preserved
    );
  }
});
