import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { createServer } from "vite";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

let server, adr, discovery, service;
before(async () => {
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  adr = await server.ssrLoadModule("/src/lib/adr-reconciliation.ts");
  discovery = await server.ssrLoadModule("/src/lib/repository-discovery.ts");
  ({ repositoryService: service } = await server.ssrLoadModule(
    "/src/api/repository-service.ts"
  ));
});
after(async () => {
  await server?.close();
});
const scope = ["alice", "token-a", "repo_a"];
const key = ["repositories", "alice", "token-a"];
const doc = (id, status, updated_at = "2026-10-18T01:00:00Z") => ({
  id,
  status,
  updated_at,
  created_at: "2026-10-17T00:00:00Z",
  source_name: id + ".md",
  source_type: "MANUAL_UPLOAD",
  title: id,
  error: null,
});
function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  client.setQueryData(
    adr.adrStateOptions(scope).queryKey,
    adr.adrStateOptions(scope).initialData()
  );
  return client;
}
const derived = [
  "architectural-knowledge",
  "architectural-knowledge-detail",
  "repository-overview",
  "repository-graph",
];
function seed(client) {
  for (const name of derived)
    client.setQueryData([name, ...scope], { old: true });
}
function observe(client, documents, started = adr.readADRState(client, scope)) {
  return adr.reconcileADRDocuments(client, scope, documents, started);
}
function stale(client) {
  return derived.map(
    (name) => client.getQueryState([name, ...scope])?.isInvalidated
  );
}
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};

test("list refresh, individual status, and remount reconcile each new completion once", async () => {
  const client = setup();
  try {
    seed(client);
    observe(client, [
      doc("a", "PENDING"),
      doc("b", "PROCESSING"),
      doc("c", "FAILED"),
    ]);
    assert.deepEqual(stale(client), [false, false, false, false]);
    const rows = await adr.fetchADRDocuments(
      client,
      scope,
      async () => [
        doc("a", "COMPLETED"),
        doc("b", "PROCESSING"),
        doc("c", "FAILED"),
      ],
      new AbortController().signal
    );
    assert.equal(rows[0].status, "COMPLETED");
    assert.deepEqual(stale(client), [true, true, true, true]);
    seed(client);
    // Another panel instance reads the same QueryClient state.
    await adr.fetchADRDocuments(
      client,
      scope,
      async () => rows,
      new AbortController().signal
    );
    observe(client, [rows[0]]); // Check Status unchanged
    assert.deepEqual(stale(client), [false, false, false, false]);
    observe(client, [doc("b", "COMPLETED"), doc("c", "COMPLETED")]);
    assert.deepEqual(stale(client), [true, true, true, true]);
    seed(client);
    observe(client, [doc("a", "COMPLETED", "2026-10-18T02:00:00Z")]);
    assert.deepEqual(stale(client), [true, true, true, true]);
  } finally {
    client.clear();
  }
});

test("a new processing cycle can complete again, while acceptance cannot imply completion", () => {
  const client = setup();
  try {
    observe(client, [doc("a", "COMPLETED")]);
    seed(client);
    observe(client, [doc("a", "PENDING", "2026-10-18T02:00:00Z")]);
    observe(client, [doc("a", "PROCESSING", "2026-10-18T02:01:00Z")]);
    assert.deepEqual(stale(client), [false, false, false, false]);
    observe(client, [doc("a", "COMPLETED", "2026-10-18T02:02:00Z")]);
    assert.deepEqual(stale(client), [true, true, true, true]);
  } finally {
    client.clear();
  }
});

test("missing or newly supplied version metadata does not replay an unchanged completion", () => {
  const client = setup();
  try {
    observe(client, [doc("a", "COMPLETED")]);
    seed(client);
    observe(client, [{ ...doc("a", "COMPLETED"), updated_at: null }]);
    assert.deepEqual(stale(client), [false, false, false, false]);
    observe(client, [{ ...doc("b", "COMPLETED"), updated_at: null }]);
    seed(client);
    observe(client, [doc("b", "COMPLETED")]);
    assert.deepEqual(stale(client), [false, false, false, false]);
  } finally {
    client.clear();
  }
});

test("late responses cannot regress newer states or revive a deleted ADR", () => {
  const client = setup();
  try {
    const original = doc("a", "PENDING");
    observe(client, [original]);
    const oldRead = adr.readADRState(client, scope);
    observe(client, [doc("a", "COMPLETED", "2026-10-18T02:00:00Z")]);
    seed(client);
    assert.equal(observe(client, [original], oldRead)[0].status, "COMPLETED");
    assert.deepEqual(stale(client), [false, false, false, false]);
    client.setQueryData(["adr-documents", ...scope], [original]);
    adr.recordADRDeletion(client, scope, original);
    assert.deepEqual(client.getQueryData(["adr-documents", ...scope]), []);
    assert.deepEqual(observe(client, [doc("a", "COMPLETED")], oldRead), []);
    assert.equal(adr.readADRState(client, scope).cleanup, "a.md");
    for (const name of derived.filter(
      (name) => name !== "repository-overview"
    )) {
      assert.equal(client.getQueryData([name, ...scope]), undefined);
    }
    assert.ok(client.getQueryData(["repository-overview", ...scope]));
  } finally {
    client.clear();
  }
});

test("unversioned late status responses and responses from a cleared session are ignored", () => {
  const client = setup();
  try {
    const a = { ...doc("a", "PENDING"), updated_at: null };
    observe(client, [a]);
    const started = adr.readADRState(client, scope);
    observe(client, [{ ...a, status: "COMPLETED" }]);
    assert.equal(observe(client, [a], started)[0].status, "COMPLETED");
    client.clear();
    client.setQueryData(
      adr.adrStateOptions(scope).queryKey,
      adr.adrStateOptions(scope).initialData()
    );
    seed(client);
    observe(client, [{ ...a, status: "COMPLETED" }], started);
    adr.recordADRDeletion(client, scope, a, true, started);
    assert.deepEqual(stale(client), [false, false, false, false]);
    assert.equal(adr.readADRState(client, scope).cleanup, null);
  } finally {
    client.clear();
  }
});

test("only the exact readiness 503 is transitional; permissions remain distinct", () => {
  const error = (status, detail) => ({
    isAxiosError: true,
    response: { status, data: { detail } },
  });
  const message =
    "Repository graph update or repair is in progress; retry later";
  assert.equal(adr.isGraphReadinessError(error(503, message)), true);
  for (const status of [401, 403, 404, 500])
    assert.equal(adr.isGraphReadinessError(error(status, message)), false);
  assert.equal(
    adr.isGraphReadinessError(error(503, "Database unavailable")),
    false
  );
  assert.equal(
    adr.canDeleteADR({
      can_manage_source_documents: true,
      can_view_settings: true,
    }),
    true
  );
  assert.equal(
    adr.canDeleteADR({
      can_manage_source_documents: true,
      can_view_settings: false,
    }),
    false
  );
  assert.equal(
    adr.canDeleteADR({
      can_manage_source_documents: false,
      can_view_settings: true,
    }),
    false
  );
});

test("deletion evicts evaluation/chat evidence only within its scope and recovery publishes fresh Overview first", () => {
  const client = setup();
  try {
    seed(client);
    const unsafe = [
      "pull-requests",
      "pull-request-detail",
      "evaluation-result",
      "explorer-conversations",
    ];
    for (const name of unsafe) {
      client.setQueryData([name, ...scope, 2], { evidence: "deleted" });
      client.setQueryData([name, "bob", "token-b", "repo_b"], { keep: true });
    }
    adr.recordADRDeletion(client, scope, doc("a", "COMPLETED"));
    const started = adr.readADRState(client, scope);
    for (const name of unsafe) {
      assert.equal(client.getQueryData([name, ...scope, 2]), undefined);
      assert.ok(client.getQueryData([name, "bob", "token-b", "repo_b"]));
    }
    // No timer, query, or optimistic success clears this state.
    assert.equal(adr.readADRState(client, scope).cleanup, "a.md");
    const fresh = { repository: { id: "repo_a" }, version: "after-cleanup" };
    adr.finishADRRecovery(client, scope, started, fresh);
    assert.equal(adr.readADRState(client, scope).cleanup, null);
    assert.equal(
      client.getQueryData(["repository-overview", ...scope]).version,
      "after-cleanup"
    );
    adr.recordADRDeletion(client, scope, doc("b", "COMPLETED"));
    adr.finishADRRecovery(client, scope, started, fresh);
    assert.equal(adr.readADRState(client, scope).cleanup, "b.md");
  } finally {
    client.clear();
  }
});

test("an ambiguous delete keeps documents and records uncertainty without claiming success", () => {
  const client = setup();
  try {
    const a = doc("a", "COMPLETED");
    client.setQueryData(["adr-documents", ...scope], [a]);
    adr.recordADRDeletion(client, scope, a, false);
    assert.equal(client.getQueryData(["adr-documents", ...scope]).length, 1);
    assert.deepEqual(adr.readADRState(client, scope).deleted, []);
    assert.equal(adr.readADRState(client, scope).uncertain, true);
  } finally {
    client.clear();
  }
});

test("disabled hidden knowledge becomes stale without fetching and refreshes on activation", async () => {
  const client = setup();
  let calls = 0;
  const options = {
    queryKey: ["architectural-knowledge", ...scope],
    queryFn: async () => ({ version: ++calls }),
    staleTime: Infinity,
    enabled: false,
  };
  client.setQueryData(options.queryKey, { version: 0 });
  const observer = new QueryObserver(client, options);
  const stop = observer.subscribe(() => {});
  try {
    observe(client, [doc("a", "COMPLETED")]);
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(calls, 0);
    assert.equal(client.getQueryState(options.queryKey).isInvalidated, true);
    observer.setOptions({ ...options, enabled: true });
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(calls, 1);
    assert.equal(observer.getCurrentResult().data.version, 1);
  } finally {
    stop();
    client.clear();
  }
});

test("concurrent card checks and dashboard share one authoritative collection request", async () => {
  const client = setup();
  const pending = deferred();
  let calls = 0;
  const original = service.getRepositories;
  service.getRepositories = async () => {
    calls++;
    return pending.promise;
  };
  try {
    const dashboard = client.fetchQuery(
      discovery.repositoryDiscoveryOptions(key)
    );
    const a = discovery.refreshRepositoryDiscovery(client, key);
    const b = discovery.refreshRepositoryDiscovery(client, key);
    pending.resolve([
      { githubRepositoryId: "a", status: "COMPLETED" },
      { githubRepositoryId: "b", status: "FAILED" },
    ]);
    const results = await Promise.all([dashboard, a, b]);
    assert.equal(calls, 1);
    assert.equal(results[1][1].status, "FAILED");
    assert.deepEqual(client.getQueryData(key), results[0]);
  } finally {
    service.getRepositories = original;
    client.clear();
  }
});

test("post-ingestion verification cancels older work and ignores late stale transport results", async () => {
  const client = setup();
  const old = deferred();
  let calls = 0;
  const original = service.getRepositories;
  service.getRepositories = async () =>
    ++calls === 1 ? old.promise : [{ status: "INDEXING" }];
  try {
    const oldRead = client
      .fetchQuery(discovery.repositoryDiscoveryOptions(key))
      .catch(() => null);
    const fresh = await discovery.refreshRepositoryDiscovery(client, key, true);
    assert.equal(fresh[0].status, "INDEXING");
    old.resolve([{ status: "NOT_INDEXED" }]);
    await oldRead;
    assert.equal(client.getQueryData(key)[0].status, "INDEXING");
    assert.equal(calls, 2);
  } finally {
    service.getRepositories = original;
    client.clear();
  }
});

test("shared collection failures reject callers and preserve the query error", async () => {
  const client = setup();
  const original = service.getRepositories;
  const failure = new Error("Discovery unavailable");
  service.getRepositories = async () => {
    throw failure;
  };
  try {
    await assert.rejects(
      discovery.refreshRepositoryDiscovery(client, key),
      /Discovery unavailable/
    );
    assert.equal(client.getQueryState(key).error, failure);
  } finally {
    service.getRepositories = original;
    client.clear();
  }
});

test("logout during the post-mutation cancellation boundary cannot recreate discovery", async () => {
  const client = setup();
  client.setQueryData(key, []);
  const request = discovery.refreshRepositoryDiscovery(client, key, true);
  client.clear();
  await assert.rejects(
    request,
    (error) => error.name === "Error" && error.message === "CancelledError"
  );
  assert.equal(client.getQueryState(key), undefined);
});
