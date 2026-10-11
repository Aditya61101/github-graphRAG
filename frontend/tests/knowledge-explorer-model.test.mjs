import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { createServer } from "vite";
import dagre from "@dagrejs/dagre";
import { layoutRegressionGraph } from "./fixtures/graph-layout-regression.mjs";
let server, model, service, client, graphLayout;
before(async () => {
  server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  model = await server.ssrLoadModule("/src/lib/explorer-model.ts");
  graphLayout = await server.ssrLoadModule("/src/lib/graph-layout.ts");
  service = (await server.ssrLoadModule("/src/api/explore-service.ts"))
    .exploreService;
  client = (await server.ssrLoadModule("/src/lib/api-client.ts")).apiClient;
  globalThis.localStorage = { getItem: () => null };
});
test("graph layout excludes absent endpoints and preserves valid directed relationships", () => {
  const graph = {
    nodes: [{ id: "a" }, { id: "b" }],
    edges: [
      { id: "valid", source: "a", target: "b" },
      { id: "reverse", source: "b", target: "a" },
      { id: "self", source: "a", target: "a" },
      { id: "dangling", source: "a", target: "missing" },
    ],
  };
  const result = graphLayout.prepareGraphLayout(graph);
  assert.equal(result.unavailableEdges, 1);
  assert.equal(result.fallback, false);
  assert.deepEqual(
    result.graph.edges.map((edge) => edge.id),
    ["valid", "reverse", "self"]
  );
  assert.equal(graph.edges.length, 4);
  for (const position of result.positions.values()) {
    assert.ok(Number.isFinite(position.x) && Number.isFinite(position.y));
  }
});
test("parallel and reverse relationships retain automatic layout and separate routed labels", () => {
  const original = structuredClone(layoutRegressionGraph);
  const result = graphLayout.prepareGraphLayout(layoutRegressionGraph);
  assert.equal(result.fallback, false);
  assert.equal(result.failureReason, null);
  assert.equal(result.routes.size, original.edges.length);
  assert.deepEqual(result.graph, original);
  const first = result.routes.get(original.edges[0].id);
  const parallel = result.routes.get(original.edges[2].id);
  assert.notDeepEqual(first.points, parallel.points);
  assert.notDeepEqual(first.label, parallel.label);
  for (const route of result.routes.values()) {
    assert.ok(
      route.points.every(
        (point) => Number.isFinite(point.x) && Number.isFinite(point.y)
      )
    );
    assert.ok(Number.isFinite(route.label.x) && Number.isFinite(route.label.y));
    assert.match(graphLayout.routedEdgePath(route.points), /^M /);
    assert.doesNotMatch(
      graphLayout.routedEdgePath(route.points),
      /NaN|undefined/
    );
  }
});
test("dense cyclic multigraphs with self loops have non-overlapping nodes and retain all edge routes", () => {
  let seed = 42;
  const random = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  for (let attempt = 0; attempt < 30; attempt++) {
    const graph = {
      nodes: layoutRegressionGraph.nodes,
      edges: Array.from({ length: 25 }, (_, index) => ({
        id: `edge:${index}`,
        source: `entity:${Math.floor(random() * 8)}`,
        target: `entity:${Math.floor(random() * 8)}`,
        type: `RELATION_${index}`,
        metadata: {},
      })),
    };
    const result = graphLayout.prepareGraphLayout(graph);
    assert.equal(result.fallback, false, result.failureReason ?? undefined);
    assert.equal(result.routes.size, graph.edges.length);
    const positions = [...result.positions.values()];
    for (let left = 0; left < positions.length; left++) {
      for (let right = left + 1; right < positions.length; right++) {
        assert.ok(
          Math.abs(positions[left].x - positions[right].x) >= 190 ||
            Math.abs(positions[left].y - positions[right].y) >= 72
        );
      }
    }
  }
});
test("rectangle intersection failures fall back to finite positions without changing graph data", () => {
  const original = dagre.layout;
  dagre.layout = () => {
    throw new Error(
      "Not possible to find intersection inside of the rectangle"
    );
  };
  try {
    const graph = {
      nodes: [{ id: "a" }, { id: "b" }],
      edges: [{ id: "edge", source: "a", target: "b" }],
    };
    const result = graphLayout.prepareGraphLayout(graph);
    assert.equal(result.fallback, true);
    assert.deepEqual(result.graph, graph);
    assert.notDeepEqual(result.positions.get("a"), result.positions.get("b"));
  } finally {
    dagre.layout = original;
  }
});
after(async () => {
  delete globalThis.localStorage;
  await server?.close();
});
test("response adapter retains evidence and unsupported assertion metadata independently for each answer", () => {
  const first = model.adaptQueryResponse({
    answer: "Storage",
    sources: [
      {
        file_path: "adr.md",
        excerpt: "Evidence",
        score: 0.9,
        score_type: "reranker_relevance",
      },
    ],
    conversation_id: "c1",
    graph_context: {
      node_ids: ["entity:db"],
      edge_ids: ["edge:a:WRITES:db"],
      assertion_ids: ["assertion:adr:1"],
    },
  });
  const second = model.adaptQueryResponse({
    answer: "No evidence",
    conversation_id: "c1",
  });
  assert.notEqual(first.id, second.id);
  assert.equal(first.content, "Storage");
  assert.equal(first.sources[0].excerpt, "Evidence");
  assert.deepEqual(first.graphContext.assertion_ids, ["assertion:adr:1"]);
  assert.deepEqual(second.sources, []);
  assert.deepEqual(second.graphContext, model.emptyContext());
});
test("matching uses exact IDs and never selects unrelated relationships between matched endpoints", () => {
  const graph = {
    nodes: [{ id: "entity:a" }, { id: "entity:b" }, { id: "entity:c" }],
    edges: [
      { id: "edge:a:WRITES:b", source: "entity:a", target: "entity:b" },
      { id: "edge:a:READS:b", source: "entity:a", target: "entity:b" },
    ],
  };
  const result = model.matchGraphContext(graph, {
    node_ids: ["entity:a", "entity:b", "missing"],
    edge_ids: ["edge:a:WRITES:b", "missing-edge"],
    assertion_ids: ["assertion:adr:1"],
  });
  assert.deepEqual([...result.nodes], ["entity:a", "entity:b"]);
  assert.deepEqual([...result.edges], ["edge:a:WRITES:b"]);
  assert.equal(result.missing, 2);
  assert.deepEqual([...result.fitNodes], ["entity:a", "entity:b"]);
  const edgeOnly = model.matchGraphContext(graph, {
    node_ids: [],
    edge_ids: ["edge:a:WRITES:b"],
    assertion_ids: [],
  });
  assert.equal(edgeOnly.nodes.size, 0);
  assert.deepEqual([...edgeOnly.fitNodes], ["entity:a", "entity:b"]);
});
test("split preferences are bounded and new conversations have isolated identifiers", () => {
  assert.equal(model.clampSplit(1), 25);
  assert.equal(model.clampSplit(90), 75);
  assert.equal(model.clampSplit(40), 40);
  const first = model.createConversationHistory(),
    second = model.createConversationHistory();
  assert.notEqual(first.activeId, second.activeId);
  assert.deepEqual(first.conversations[0].messages, []);
});
test("query service scopes URLs, carries cancellation, preserves conversation IDs and rejects mismatched responses", async () => {
  let config;
  const controller = new AbortController();
  client.defaults.adapter = async (options) => {
    config = options;
    return {
      data: {
        answer: "answer",
        conversation_id: "c1",
        sources: [],
        graph_context: model.emptyContext(),
      },
      status: 200,
      statusText: "OK",
      headers: {},
      config: options,
    };
  };
  await service.sendMessage("repo_101", "c1", "Question", controller.signal);
  assert.equal(config.url, "/repositories/repo_101/query");
  assert.deepEqual(JSON.parse(config.data), {
    query: "Question",
    conversation_id: "c1",
  });
  assert.equal(config.signal, controller.signal);
  await assert.rejects(
    () => service.sendMessage("repo_101", "c2", "Question"),
    /different conversation/
  );
});
