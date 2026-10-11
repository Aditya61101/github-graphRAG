import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";

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
const allowed = {
  github_admin: true,
  can_enable_monitoring: true,
  can_retry_indexing: true,
  reason: "allowed",
};
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
    createElement(
      MemoryRouter,
      null,
      createElement(Card, {
        repository,
        pending: false,
        onIndex() {},
        ...props,
      })
    )
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
      /Enable Monitoring|Retry Monitoring|Reindex|Upload ADR|[/]projects[/]/
    );
    assert.match(html, /View on GitHub/);
  }
  assert.equal(
    flow.getRepositoryAction({
      ...repository,
      trackedRepositoryId: "repo_101",
      monitoring: allowed,
    }),
    "enable"
  );
  assert.equal(
    flow.getRepositoryAction({
      ...repository,
      trackedRepositoryId: "repo_101",
      status: "FAILED",
      monitoring: allowed,
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

test("cards expose real branch and state with canonical workspace links and no fabricated statistics", () => {
  const html = render({
    repository: {
      ...repository,
      id: "repo_101",
      monitoring: allowed,
      trackedRepositoryId: "repo_101",
      trackedBranch: "develop",
      status: "COMPLETED",
    },
  });
  assert.match(html, /Monitored|develop|Public|example-org/);
  assert.doesNotMatch(html, /Enable Monitoring|ADR records|Entities/);
  assert.match(html, /href="[/]projects[/]repo_101[/]overview"/);
  assert.match(html, /Open Repository/);
  assert.doesNotMatch(html, /View Status|View Access/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(render({ error: "Access denied" }), /role="alert"/);
});

test("ready card navigation requires allowed monitoring and the canonical internal ID", () => {
  for (const reason of [
    "permission_unknown",
    "github_admin_required",
    "owned_by_another_user",
    "installation_binding_mismatch",
  ]) {
    const html = render({
      repository: {
        ...repository,
        id: "repo_101",
        trackedRepositoryId: "repo_101",
        status: "COMPLETED",
        monitoring: { ...allowed, reason },
      },
    });
    assert.match(html, /View Access/);
    assert.doesNotMatch(
      html,
      /Open Repository|View Status|href="[/]projects[/]/
    );
  }
  const inconsistent = render({
    repository: {
      ...repository,
      trackedRepositoryId: "repo_101",
      status: "COMPLETED",
      monitoring: allowed,
    },
  });
  assert.match(inconsistent, /View Access/);
  assert.doesNotMatch(inconsistent, /Open Repository/);
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
    /permission|tracked repository/
  );
  assert.equal(calls.length, 0);
  await service.syncRepository({
    ...repository,
    trackedRepositoryId: "repo_101",
    monitoring: allowed,
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
    service.syncRepository({
      ...repository,
      trackedRepositoryId: "repo_101",
      monitoring: allowed,
    }),
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

test("verified administrator can enable an untracked repo without any username inference", async () => {
  const item = {
    ...repository,
    fullName: "Aditya61101/Aditya61101",
    name: "Aditya61101",
    monitoring: allowed,
  };
  assert.equal(flow.getRepositoryAction(item), "enable");
  assert.equal(flow.getRepositoryState(item), "available");
  assert.match(render({ repository: item }), /Enable Monitoring/);
  assert.doesNotMatch(
    render({ repository: item }),
    /Monitoring permissions are not provided/
  );
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
  await service.enableMonitoring(item);
  assert.equal(calls[0].url, "/repositories/ingest");
  assert.deepEqual(JSON.parse(calls[0].data), {
    repository: item.fullName,
    installation_id: item.installationId,
    branch: "main",
    force_full: false,
    sync: false,
  });
});

test("unknown permission is distinct from explicit restriction, even when owner matches the user", async () => {
  for (const monitoring of [
    undefined,
    {
      github_admin: null,
      can_enable_monitoring: null,
      can_retry_indexing: false,
      reason: "permission_unknown",
    },
  ]) {
    const item = { ...repository, fullName: "alice/profile", monitoring };
    assert.equal(flow.getRepositoryState(item), "available");
    assert.equal(flow.getRepositoryAction(item), null);
    assert.match(
      render({ repository: item }),
      /permissions could not be verified/
    );
    assert.doesNotMatch(render({ repository: item }), /Restricted/);
    await assert.rejects(
      service.enableMonitoring(item),
      /permission has not been verified/
    );
  }
  for (const reason of [
    "github_admin_required",
    "owned_by_another_user",
    "installation_binding_mismatch",
  ]) {
    const item = {
      ...repository,
      monitoring: { ...allowed, can_enable_monitoring: false, reason },
    };
    assert.equal(flow.getRepositoryState(item), "restricted");
    assert.equal(flow.getRepositoryAction(item), null);
    assert.match(render({ repository: item }), /Restricted/);
  }
  // Even an inconsistent response cannot make non-admins eligible.
  assert.equal(
    flow.getRepositoryAction({
      ...repository,
      monitoring: { ...allowed, github_admin: false },
    }),
    null
  );
});

test("monitoring errors preserve explicit admin denial and unverified permissions", () => {
  for (const [status, detail] of [
    [
      403,
      "GitHub repository administrator permission is required to enable or retry monitoring",
    ],
    [
      503,
      "GitHub repository administration permission could not be verified; retry discovery",
    ],
  ]) {
    assert.equal(
      flow.getMonitoringActionError({
        isAxiosError: true,
        response: { status, data: { detail } },
      }),
      detail
    );
  }
  assert.equal(
    flow.getMonitoringActionError(new Error("network")),
    "Could not start indexing. Please try again."
  );
});

test("lifecycle states remain separate from administration rights", () => {
  for (const [status, expected] of [
    ["COMPLETED", "ready"],
    ["INDEXING", "indexing"],
    ["FAILED", "failed"],
  ]) {
    for (const reason of [
      "github_admin_required",
      "permission_unknown",
      "installation_binding_mismatch",
    ]) {
      const item = {
        ...repository,
        trackedRepositoryId: "repo_101",
        status,
        monitoring: { ...allowed, github_admin: false, reason },
      };
      assert.equal(flow.getRepositoryState(item), expected);
      assert.equal(flow.getRepositoryAction(item), null);
      const html = render({ repository: item, onCheckStatus() {} });
      assert.doesNotMatch(
        html,
        /Enable Monitoring|Retry Monitoring|Repository indexing completed successfully/
      );
      assert.match(
        html,
        status === "INDEXING"
          ? /Check Status/
          : status === "FAILED"
            ? /View Error/
            : /View Access/
      );
    }
  }
});

test("manual status uses the real discovery endpoint and matches GitHub identity", async () => {
  const calls = [];
  const dto = {
    id: "repo_101",
    github_repository_id: "101",
    installation_id: "10",
    full_name: repository.fullName,
    name: repository.name,
    repository_url: repository.repositoryUrl,
    default_branch: "main",
    tracked_branch: "main",
    is_private: false,
    description: null,
    status: "COMPLETED",
    tracked_repository_id: "repo_101",
    indexed_commit_sha: "abc",
    updated_at: null,
    monitoring: allowed,
  };
  client.defaults.adapter = async (config) => {
    calls.push(config);
    return {
      data: [dto, { ...dto, github_repository_id: "102", status: "FAILED" }],
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
  };
  const latest = await service.getRepositoryStatus(repository);
  assert.equal(latest.status, "COMPLETED");
  assert.equal(latest.githubRepositoryId, "101");
  assert.equal(calls[0].method, "get");
  assert.equal(calls[0].url, "/repositories");
  client.defaults.adapter = async (config) => ({
    data: [],
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  });
  await assert.rejects(
    service.getRepositoryStatus(repository),
    /access is no longer available/
  );
});
