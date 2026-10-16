import assert from "node:assert/strict";
import { before, after, beforeEach, test } from "node:test";
import { createServer } from "vite";
import { openBrowser } from "./browser-helper.mjs";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { layoutRegressionGraph } from "./fixtures/graph-layout-regression.mjs";

let browser, server, base, fixture, screenshots;
const token =
  "e30." +
  Buffer.from(
    JSON.stringify({ sub: "usr_alice", exp: Date.now() / 1000 + 36000 })
  ).toString("base64url") +
  ".test";
const graph = {
  nodes: [
    {
      id: "entity:orders",
      label: "OrderService",
      type: "Service",
      metadata: { description: "Handles orders", aliases: ["orders"] },
    },
    { id: "entity:db", label: "PostgreSQL", type: "Database", metadata: {} },
    { id: "entity:events", label: "EventBus", type: "Component", metadata: {} },
  ],
  edges: [
    {
      id: "edge:orders:WRITES_TO:db",
      source: "entity:orders",
      target: "entity:db",
      type: "WRITES_TO",
      metadata: {
        confidence: 0.95,
        sources: ["ADR"],
        rationale: "Transactional order persistence",
      },
    },
    {
      id: "edge:orders:READS_FROM:db",
      source: "entity:orders",
      target: "entity:db",
      type: "READS_FROM",
      metadata: { sources: ["code"] },
    },
    {
      id: "edge:orders:PUBLISHES_TO:events",
      source: "entity:orders",
      target: "entity:events",
      type: "PUBLISHES_TO",
      metadata: {},
    },
  ],
};
const unavailable = { status: "unavailable", reason: "engine_not_implemented" };
function setup() {
  return {
    calls: [],
    graphError: null,
    graphOverride: null,
    queryError: null,
    graphDelay: 0,
    queryDelay: 0,
    empty: false,
    denied: false,
    answers: 0,
  };
}
before(async () => {
  fixture = setup();
  server = await createServer({
    server: { host: "127.0.0.1", port: 0, hmr: false },
    define: {
      "import.meta.env.VITE_BACKEND_URL": JSON.stringify("/__explorer_api"),
    },
    plugins: [
      {
        name: "explorer-test-api",
        configureServer(vite) {
          vite.middlewares.use("/__explorer_api", async (req, res) => {
            const f = fixture;
            const call = {
              url: req.url,
              method: req.method,
              bearer: req.headers.authorization,
            };
            f.calls.push(call);
            res.setHeader("Content-Type", "application/json");
            const json = (body, status = 200) => {
              res.statusCode = status;
              res.end(JSON.stringify(body));
            };
            if (req.url === "/auth/me")
              return json({
                onboarding_completed: true,
                next: "/projects",
                user: { id: "usr_alice", username: "alice" },
                installations: [],
                installation_discovery_status: "not_requested",
                reconnect_required: false,
                installation_onboarding_required: false,
              });
            if (f.denied)
              return json({ detail: "Repository access denied" }, 403);
            const repositoryId = req.url.split("/")[2];
            if (req.url.endsWith("/overview"))
              return json({
                repository: {
                  id: repositoryId,
                  owner: "owner",
                  name: repositoryId,
                  full_name: `owner/${repositoryId}`,
                  repository_url: "https://github.com/owner/repo",
                  tracked_branch: "main",
                  status: "COMPLETED",
                },
                permissions: {
                  can_view_settings: false,
                  can_manage_source_documents: false,
                },
                kpis: {
                  architecture_decisions: {
                    value: 1,
                    availability: { status: "available" },
                  },
                  decisions_at_risk: { value: null, availability: unavailable },
                  prs_analyzed: { value: null, availability: unavailable },
                  architectural_warnings: {
                    value: null,
                    availability: unavailable,
                  },
                },
                review_activity: { availability: unavailable, window_days: 7 },
                decisions_needing_attention: { availability: unavailable },
                recent_pr_reviews: { availability: unavailable },
              });
            if (req.url.endsWith("/graph")) {
              if (f.graphDelay)
                await new Promise((resolve) =>
                  setTimeout(resolve, f.graphDelay)
                );
              if (f.graphError)
                return json(
                  { detail: "Graph temporarily unavailable" },
                  f.graphError
                );
              if (f.empty) return json({ nodes: [], edges: [] });
              if (f.graphOverride) return json(f.graphOverride);
              return json(
                repositoryId === "repo_b"
                  ? {
                      nodes: [
                        {
                          id: "entity:other",
                          label: "OtherService",
                          type: "Service",
                          metadata: {},
                        },
                      ],
                      edges: [],
                    }
                  : graph
              );
            }
            if (req.url.endsWith("/query")) {
              let raw = "";
              for await (const chunk of req) raw += chunk;
              call.body = JSON.parse(raw);
              if (f.queryDelay)
                await new Promise((resolve) =>
                  setTimeout(resolve, f.queryDelay)
                );
              if (f.queryError)
                return json({ detail: "Query unavailable" }, f.queryError);
              f.answers++;
              const second = f.answers === 2,
                empty = f.answers > 2;
              return json({
                answer: empty
                  ? "No matching evidence."
                  : second
                    ? "Events are published to **EventBus**."
                    : "## Transactional storage\n\nUse **PostgreSQL** for orders.\n\n- Transactions preserve consistency.\n\n`OrderService` writes orders.",
                conversation_id: call.body.conversation_id,
                sources: empty
                  ? []
                  : [
                      {
                        file_path: second
                          ? "docs/events.md"
                          : "docs/storage.md",
                        excerpt: second
                          ? "Publish order events after persistence."
                          : "Persist orders in PostgreSQL using transactions.",
                        score: 0.94,
                        score_type: "reranker_relevance",
                      },
                    ],
                graph_context: empty
                  ? { node_ids: [], edge_ids: [], assertion_ids: [] }
                  : {
                      node_ids: second
                        ? ["entity:events"]
                        : ["entity:orders", "entity:db", "entity:missing"],
                      edge_ids: second
                        ? ["edge:orders:PUBLISHES_TO:events"]
                        : ["edge:orders:WRITES_TO:db", "edge:missing"],
                      assertion_ids: ["assertion:adr:storage"],
                    },
              });
            }
            return json({ detail: "Not found" }, 404);
          });
        },
      },
    ],
  });
  await server.listen();
  base = "http://127.0.0.1:" + server.httpServer.address().port;
  browser = await openBrowser();
  await browser.call("Page.addScriptToEvaluateOnNewDocument", {
    source: `localStorage.setItem('token',${JSON.stringify(token)});localStorage.setItem('theme','dark');Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copiedAnswer=text;}}});`,
  });
  screenshots = await mkdtemp(
    path.join(tmpdir(), "decisionguard-explorer-qa-")
  );
});
beforeEach(async () => {
  fixture = setup();
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 1100,
    deviceScaleFactor: 1,
    mobile: false,
  });
});
after(async () => {
  await browser?.close();
  await server?.close();
});
const contains = (text) =>
  `document.body.innerText.includes(${JSON.stringify(text)})`;
async function visit(id = "repo_a") {
  await browser.navigate(base + `/projects/${id}/explore`);
  await browser.waitFor(contains("Architecture Chat"));
}
async function readyGraph() {
  await browser.waitFor(
    "document.querySelectorAll('.react-flow__node').length===3"
  );
}
async function click(label, index = 0) {
  await browser.evaluate(
    `Array.from(document.querySelectorAll('button')).filter(b=>b.textContent.trim()===${JSON.stringify(label)})[${index}].click()`
  );
}
async function send(text) {
  await browser.evaluate(
    `(()=>{const input=document.querySelector('#explorer-question');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,${JSON.stringify(text)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`
  );
  await click("Send");
  await browser.waitFor(
    "!document.querySelector('#explorer-question').disabled || document.body.innerText.includes('Repository chat access denied.')"
  );
}
async function link(route) {
  await browser.evaluate(
    `document.querySelector('a[href="/projects/repo_a/${route}"]').click()`
  );
}
async function capture(name) {
  const result = await browser.call("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
  });
  await writeFile(
    path.join(screenshots, name),
    Buffer.from(result.data, "base64")
  );
}

test("chat continuity, Markdown, per-answer evidence and exact graph highlighting", async () => {
  await visit();
  await readyGraph();
  await send("Explain storage");
  await browser.waitFor(contains("Transactional storage"));
  assert.equal(
    await browser.evaluate(
      "!!document.querySelector('[data-slot=chat-messages] h2')"
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[data-slot=chat-messages] strong').textContent"
    ),
    "PostgreSQL"
  );
  await click("Sources (1)");
  await browser.waitFor(contains("docs/storage.md"));
  assert.equal(await browser.evaluate(contains("0.94")), false);
  await click("Copy");
  await browser.waitFor(contains("Copied answer"));
  assert.match(
    await browser.evaluate("window.copiedAnswer"),
    /Transactional storage/
  );
  await click("View in Graph");
  await browser.waitFor(contains("Answer Context"));
  assert.deepEqual(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('.answer-context-node')).map(e=>e.getAttribute('data-id')).sort()"
    ),
    ["entity:db", "entity:orders"]
  );
  // React Flow mounts the SVG edges after its node measurement pass.
  await browser.waitFor(
    "document.querySelector('.answer-context-edge')?.getAttribute('data-id') === 'edge:orders:WRITES_TO:db'"
  );
  assert.deepEqual(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('.answer-context-edge')).map(e=>e.getAttribute('data-id'))"
    ),
    ["edge:orders:WRITES_TO:db"]
  );
  assert.equal(
    await browser.evaluate(contains("2 unavailable identifiers")),
    true
  );
  await send("What about events?");
  await browser.waitFor(contains("Events are published"));
  await click("View in Graph", 1);
  await browser.waitFor(
    "document.querySelector('.answer-context-node')?.getAttribute('data-id')==='entity:events'"
  );
  await click("View in Graph", 0);
  await browser.waitFor(
    "document.querySelectorAll('.answer-context-node').length===2"
  );
  await click("Clear Highlight");
  await browser.waitFor(
    "document.querySelectorAll('.answer-context-node,.answer-context-edge').length===0"
  );
  const calls = fixture.calls.filter((c) => c.method === "POST");
  assert.equal(calls.length, 2);
  assert.equal(calls[0].body.conversation_id, calls[1].body.conversation_id);
  assert.equal(calls[0].url, "/repositories/repo_a/query");
  assert.equal(calls[0].body.query, "Explain storage");
  assert.equal(calls[0].bearer, `Bearer ${token}`);
  await capture("explorer-desktop.png");
});

test("graph node/edge inspectors, zoom, fit and layout persistence do not refetch", async () => {
  await visit();
  await readyGraph();
  await browser.evaluate(
    "document.querySelector('.react-flow__node[data-id=\"entity:orders\"]').click()"
  );
  await browser.waitFor(contains("Handles orders"));
  await click("Close details");
  await browser.evaluate(
    "document.querySelector('button[aria-label=\"Inspect WRITES_TO relationship\"]').click()"
  );
  await browser.waitFor(contains("Transactional order persistence"));
  assert.equal(await browser.evaluate(contains("confidence")), true);
  await click("Close details");
  await click("Fit graph");
  await click("Reset graph");
  await browser.evaluate("document.querySelector('[role=separator]').focus()");
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "End",
    code: "End",
    windowsVirtualKeyCode: 35,
  });
  await browser.waitFor(
    "document.querySelector('[role=separator]').getAttribute('aria-valuenow')==='75'"
  );
  assert.equal(fixture.calls.filter((c) => c.url.endsWith("/graph")).length, 1);
  await browser.navigate(base + "/projects/repo_a/explore");
  await readyGraph();
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[role=separator]').getAttribute('aria-valuenow')"
    ),
    "75"
  );
  await click("Reset Layout");
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[role=separator]').getAttribute('aria-valuenow')"
    ),
    "40"
  );
  const bounds = await browser.evaluate(
    "(()=>{const r=document.querySelector('[role=separator]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+20};})()"
  );
  await browser.call("Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: bounds.x,
    y: bounds.y,
    button: "left",
    clickCount: 1,
  });
  await browser.call("Input.dispatchMouseEvent", {
    type: "mouseMoved",
    x: bounds.x + 120,
    y: bounds.y,
    button: "left",
    buttons: 1,
  });
  await browser.call("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: bounds.x + 120,
    y: bounds.y,
    button: "left",
    clickCount: 1,
  });
  assert.ok(
    Number(
      await browser.evaluate(
        "document.querySelector('[role=separator]').getAttribute('aria-valuenow')"
      )
    ) > 40
  );
  assert.equal(fixture.calls.filter((c) => c.url.endsWith("/graph")).length, 2);
});

test("conversation switching clears highlight and client navigation retains isolated history", async () => {
  await visit();
  await readyGraph();
  await send("Original question");
  await browser.waitFor(contains("Transactional storage"));
  const first = fixture.calls.find((c) => c.method === "POST").body
    .conversation_id;
  await click("View in Graph");
  await browser.waitFor(contains("Answer Context"));
  await click("New");
  assert.equal(await browser.evaluate(contains("Answer Context")), false);
  await send("Second conversation");
  await browser.waitFor(contains("Events are published"));
  assert.notEqual(
    fixture.calls.filter((c) => c.method === "POST")[1].body.conversation_id,
    first
  );
  await browser.evaluate(
    `(()=>{const select=document.querySelector('#explorer-conversation');select.value=${JSON.stringify(first)};select.dispatchEvent(new Event('change',{bubbles:true}));})()`
  );
  await browser.waitFor(contains("Original question"));
  assert.equal(await browser.evaluate(contains("Events are published")), false);
  await link("overview");
  await browser.waitFor(contains("Architecture Overview"));
  await link("explore");
  await browser.waitFor(contains("Original question"));
  assert.equal(fixture.calls.filter((c) => c.url.endsWith("/graph")).length, 1);
  await send("Follow up");
  await browser.waitFor(contains("No matching evidence."));
  assert.equal(
    fixture.calls.filter((c) => c.method === "POST").at(-1).body
      .conversation_id,
    first
  );
  await click("Sources (0)");
  await browser.waitFor(contains("No supporting sources returned."));
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('button')).filter(b=>b.textContent==='View in Graph').at(-1).disabled"
    ),
    true
  );
});

test("graph failures and query retries remain independent; pending sends cannot duplicate", async () => {
  fixture.graphError = 503;
  fixture.queryDelay = 400;
  fixture.queryError = 503;
  await visit();
  await browser.waitFor(contains("Graph temporarily unavailable"));
  await send("Retry query");
  await browser.waitFor(contains("Query unavailable"));
  assert.equal(fixture.calls.filter((c) => c.method === "POST").length, 1);
  fixture.queryError = null;
  await click("Retry last question");
  await browser.waitFor(contains("Transactional storage"));
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('[data-message-id]').length"
    ),
    2
  );
  fixture.graphError = null;
  await click("Retry graph");
  await readyGraph();
  fixture.queryError = 403;
  await send("Revoked query");
  await browser.waitFor(contains("Repository chat access denied."));
  assert.equal(
    await browser.evaluate(contains("Transactional storage")),
    false
  );
});

test("missing relationship endpoints cannot crash Explorer; sidebar collapses on entry and stays toggleable", async () => {
  fixture.graphOverride = {
    ...graph,
    edges: [
      ...graph.edges,
      {
        id: "edge:orders:WRITES_TO:missing",
        source: "entity:orders",
        target: "entity:missing",
        type: "WRITES_TO",
        metadata: {},
      },
    ],
  };
  await visit();
  await readyGraph();
  await browser.waitFor(contains("1 relationships have unavailable endpoints"));
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[data-slot=sidebar]').dataset.state"
    ),
    "collapsed"
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('.react-flow__edge').length"
    ),
    3
  );
  await browser.click("Toggle Sidebar");
  await browser.waitFor(
    "document.querySelector('[data-slot=sidebar]').dataset.state === 'expanded'"
  );
  await send("Still usable");
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[data-slot=sidebar]').dataset.state"
    ),
    "expanded"
  );
  await link("overview");
  await browser.waitFor(contains("Architecture Overview"));
  await link("explore");
  await browser.waitFor(
    "document.querySelector('[data-slot=sidebar]').dataset.state === 'collapsed'"
  );
  await readyGraph();
});

test("Explorer fills the workspace viewport and cyclic parallel graph retains automatic routing", async () => {
  fixture.graphOverride = layoutRegressionGraph;
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 1600,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await visit();
  await browser.waitFor(
    "document.querySelectorAll('.react-flow__node').length === 8"
  );
  assert.equal(
    await browser.evaluate(contains("Automatic layout unavailable")),
    false
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[aria-label=\"Repository identity\"]') === null"
    ),
    true
  );
  assert.equal(await browser.evaluate(contains("View on GitHub")), false);
  assert.equal(
    await browser.evaluate(`(() => {
    const split = document.querySelector('[data-slot="explorer-split"]').getBoundingClientRect();
    const composer = document.querySelector('#explorer-question').getBoundingClientRect();
    return split.width > innerWidth - 120 && split.bottom <= innerHeight && composer.bottom <= split.bottom && document.documentElement.scrollHeight <= innerHeight + 1;
  })()`),
    true
  );
  await capture("explorer-wide-routed-graph.png");
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 1024,
    height: 700,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await browser.waitFor(`(() => {
    const canvas = document.querySelector('[data-slot="graph-canvas"]').getBoundingClientRect();
    const flow = document.querySelector('.react-flow').getBoundingClientRect();
    return Math.abs(canvas.width - flow.width) < 1 && Math.abs(canvas.height - flow.height) < 1 && document.documentElement.scrollHeight <= innerHeight + 1;
  })()`);
  await click("Fit graph");
  await browser.evaluate("document.querySelector('.react-flow__node').click()");
  await browser.waitFor("document.querySelector('[aria-label=\"Graph details\"]') !== null");
  assert.equal(
    fixture.calls.filter((call) => call.url.endsWith("/graph")).length,
    1
  );
});

test("mobile Chat/Graph tabs preserve messages and fit graph without extra fetching", async () => {
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  await visit();
  assert.equal(
    await browser.evaluate(
      "getComputedStyle(document.querySelector('#explorer-graph-panel')).display"
    ),
    "none"
  );
  await send("Mobile question");
  await browser.waitFor(contains("Transactional storage"));
  await click("View in Graph");
  await browser.waitFor(
    "document.querySelector('#explorer-graph-tab').getAttribute('aria-selected')==='true'"
  );
  await readyGraph();
  await browser.waitFor(`(() => {
    const canvas = document.querySelector('[data-slot="graph-canvas"]').getBoundingClientRect();
    const node = Array.from(document.querySelectorAll('.react-flow__node')).find(n => n.textContent.includes('OrderService')).getBoundingClientRect();
    return node.width > 60 && node.left >= canvas.left && node.right <= canvas.right && node.top >= canvas.top && node.bottom <= canvas.bottom;
  })()`);
  assert.equal(
    await browser.evaluate(
      "getComputedStyle(document.querySelector('#explorer-chat-panel')).display"
    ),
    "none"
  );
  assert.equal(
    await browser.evaluate("document.documentElement.scrollWidth<=innerWidth"),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.documentElement.scrollHeight <= innerHeight + 1"
    ),
    true
  );
  await capture("explorer-mobile-graph.png");
  await click("Chat");
  await browser.waitFor(contains("Mobile question"));
  await capture("explorer-mobile-chat.png");
  assert.equal(fixture.calls.filter((c) => c.url.endsWith("/graph")).length, 1);
  console.log("Explorer visual checks:", screenshots);
});

test("empty graph and permission-denied workspace do not expose repository data", async () => {
  fixture.empty = true;
  await visit();
  await browser.waitFor(contains("No graph entities yet"));
  await send("Empty graph question");
  await browser.waitFor(contains("Transactional storage"));
  await click("View in Graph");
  await browser.waitFor(contains("no available graph matches"));
  await click("Clear Highlight");
  fixture.denied = true;
  await browser.navigate(base + "/projects/repo_a/explore");
  await browser.waitFor(contains("Repository access denied"));
  assert.equal(
    await browser.evaluate(contains("Transactional storage")),
    false
  );
});

test("a route change cancels in-flight responses and separates repository graph and chat", async () => {
  fixture.queryDelay = 800;
  await visit();
  await readyGraph();
  await browser.evaluate(
    "(()=>{const input=document.querySelector('#explorer-question');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,'Old repository question');input.dispatchEvent(new Event('input',{bubbles:true}));})()"
  );
  await click("Send");
  await browser.waitFor(contains("Finding an answer..."));
  await browser.evaluate(
    "history.pushState({},'', '/projects/repo_b/explore');window.dispatchEvent(new PopStateEvent('popstate'))"
  );
  await browser.waitFor(contains("OtherService"));
  assert.equal(
    await browser.evaluate(contains("Old repository question")),
    false
  );
  await browser.waitFor(
    "!document.querySelector('#explorer-question').disabled"
  );
  fixture.queryDelay = 0;
  await send("New repository question");
  await browser.waitFor(
    "document.querySelectorAll('[data-message-id]').length===2"
  );
  assert.equal(
    fixture.calls.filter((c) => c.method === "POST").at(-1).url,
    "/repositories/repo_b/query"
  );
});
