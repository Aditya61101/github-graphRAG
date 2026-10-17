import assert from "node:assert/strict";
import { before, after, beforeEach, test } from "node:test";
import { createServer } from "vite";
import { openBrowser } from "./browser-helper.mjs";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

let server, browser, base, fixture, screenshots;
const source = {
  id: "adr_a",
  title: "Storage ADR",
  source_name: "storage.md",
  source_type: "MANUAL_UPLOAD",
  source_version: "1",
  content_hash: "adr-hash",
};
const decision = {
  id: "decision_a",
  kind: "decision",
  title: "Use PostgreSQL",
  description: "Persist orders in PostgreSQL.",
  type: "technology",
  sources: [source],
};
const constraint = {
  ...decision,
  id: "constraint_a",
  kind: "constraint",
  title: null,
  description: "Encrypt order data.",
};
const doc = {
  ...source,
  status: "COMPLETED",
  created_at: "2026-10-11T00:00:00Z",
  error: null,
};
const unavailable = { status: "unavailable", reason: "engine_not_implemented" };
function setup() {
  return {
    owner: true,
    denied: false,
    calls: [],
    uploaded: false,
    documents: null,
    statusDelay: 0,
    retryError: 0,
    deleteError: false,
    deleteDelay: 0,
    sequential: false,
    duplicate: false,
    uploadError: false,
    partial: false,
    release: null,
    detail: null,
    detailError: false,
    attention: { availability: unavailable },
    items: [decision, constraint],
    knowledgeDelay: 0,
    knowledgeError: null,
  };
}
before(async () => {
  fixture = setup();
  server = await createServer({
    server: { host: "127.0.0.1", port: 0, hmr: false },
    define: {
      "import.meta.env.VITE_BACKEND_URL": JSON.stringify("/__decisions_api"),
    },
    plugins: [
      {
        name: "decisions-test-api",
        configureServer(vite) {
          vite.middlewares.use("/__decisions_api", async (req, res) => {
            const f = fixture;
            f.calls.push({
              url: req.url,
              method: req.method,
              type: req.headers["content-type"],
            });
            res.setHeader("Content-Type", "application/json");
            const json = (data, status = 200) => {
              res.statusCode = status;
              res.end(JSON.stringify(data));
            };
            const url = new URL(req.url, "http://test");
            if (url.pathname === "/auth/me")
              return json({
                onboarding_completed: true,
                next: "/projects",
                user: { id: "alice", username: "alice" },
                installations: [],
                installation_discovery_status: "not_requested",
                reconnect_required: false,
                installation_onboarding_required: false,
              });
            if (f.denied)
              return json({ detail: "Collaborator access revoked" }, 403);
            if (
              url.pathname.startsWith("/repositories/") &&
              !url.pathname.startsWith("/repositories/repo_a/")
            )
              return json({ detail: "Repository access denied" }, 403);
            if (url.pathname.endsWith("/overview"))
              return json({
                repository: {
                  id: "repo_a",
                  owner: "alice",
                  name: "api",
                  full_name: "alice/api",
                  repository_url: "https://github.com/alice/api",
                  tracked_branch: "main",
                  status: "COMPLETED",
                },
                permissions: {
                  can_view_settings: false,
                  can_manage_source_documents: f.owner,
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
                decisions_needing_attention: f.attention,
                recent_pr_reviews: { availability: unavailable },
              });
            if (url.pathname.endsWith("/knowledge")) {
              if (f.knowledgeDelay)
                await new Promise((resolve) =>
                  setTimeout(resolve, f.knowledgeDelay)
                );
              if (f.knowledgeError)
                return json(
                  { detail: "Knowledge temporarily unavailable" },
                  f.knowledgeError
                );
              let items = f.items;
              const kind = url.searchParams.get("kind");
              const search = url.searchParams.get("search")?.toLowerCase();
              if (kind && kind !== "all")
                items = items.filter((i) => i.kind === kind);
              if (search)
                items = items.filter((i) =>
                  ((i.title || "") + i.description)
                    .toLowerCase()
                    .includes(search)
                );
              const offset = Number(url.searchParams.get("offset") || 0);
              return json({
                items: items.slice(offset, offset + 20),
                total: items.length,
                offset,
                limit: 20,
              });
            }
            if (url.pathname.endsWith("/knowledge/decision_a")) {
              if (f.detailError)
                return json(
                  { detail: "Evidence temporarily unavailable" },
                  503
                );
              return json({
                ...decision,
                entities: [
                  {
                    id: "orders",
                    name: "Orders",
                    label: "Service",
                    relationship: "AFFECTS",
                    source: "ADR",
                  },
                ],
                evidence: [
                  {
                    id: "chunk_a",
                    adr_id: "adr_a",
                    section: "Decision",
                    chunk_index: 0,
                    excerpt: "PostgreSQL provides transactional persistence.",
                    truncated: false,
                    content_hash: "chunk-hash",
                  },
                ],
                entities_truncated: false,
                evidence_truncated: false,
                relationship_basis: "documented_ADR_knowledge",
                ...f.detail,
              });
            }
            if (url.pathname.endsWith("/knowledge/constraint_a"))
              return json({
                ...constraint,
                entities: [
                  {
                    id: "orders",
                    name: "Orders",
                    label: "Service",
                    relationship: "CONSTRAINS",
                    source: "ADR",
                  },
                ],
                evidence: [],
                entities_truncated: false,
                evidence_truncated: false,
                relationship_basis: "documented_ADR_knowledge",
              });
            const deleteAdr = url.pathname.match(/\/adrs\/([^/]+)$/);
            if (deleteAdr && req.method === "DELETE") {
              if (!f.owner) return json({ detail: "Owner only" }, 403);
              if (f.deleteDelay)
                await new Promise((resolve) =>
                  setTimeout(resolve, f.deleteDelay)
                );
              if (f.deleteError)
                return json({ detail: "Deletion unavailable" }, 503);
              f.documents = (f.documents ?? []).filter(
                (item) => item.id !== deleteAdr[1]
              );
              return json({ adr_id: deleteAdr[1], status: "DELETED" }, 202);
            }
            const adrAction = url.pathname.match(
              /\/adrs\/([^/]+)\/(status|retry)$/
            );
            if (adrAction) {
              const entry = f.documents?.find(
                (item) => item.id === adrAction[1]
              );
              if (!entry) return json({ detail: "ADR not found" }, 404);
              if (adrAction[2] === "retry") {
                if (f.retryError)
                  return json(
                    {
                      detail:
                        f.retryError === 403
                          ? "Repository access denied"
                          : "ADR already completed",
                    },
                    f.retryError
                  );
                entry.status = "PENDING";
                entry.error = null;
              }
              if (f.statusDelay)
                await new Promise((resolve) =>
                  setTimeout(resolve, f.statusDelay)
                );
              return json(
                {
                  adr_id: entry.id,
                  status: entry.status,
                  error: entry.error,
                  updated_at: "2026-10-17T07:00:00Z",
                },
                adrAction[2] === "retry" ? 202 : 200
              );
            }
            if (url.pathname.endsWith("/adrs")) {
              if (!f.owner) return json({ detail: "Owner only" }, 403);
              if (req.method === "GET")
                return json(
                  f.documents ??
                    (f.uploaded ? [{ ...doc, status: "PENDING" }] : [])
                );
              for await (const chunk of req) {
                assert.ok(chunk.length);
              }
              if (f.sequential || f.duplicate) {
                f.documents ??= [];
                const entry = f.duplicate
                  ? f.documents[0]
                  : {
                      ...doc,
                      id: `adr_${f.documents.length + 1}`,
                      source_name: `adr-${f.documents.length + 1}.md`,
                      status: "PENDING",
                    };
                if (!f.duplicate) f.documents.push(entry);
                return json(
                  {
                    repository_id: "repo_a",
                    total: 1,
                    completed: 0,
                    accepted: f.duplicate ? 0 : 1,
                    duplicates: f.duplicate ? 1 : 0,
                    failed: 0,
                    results: [
                      {
                        index: 0,
                        filename: "storage.md",
                        status: f.duplicate ? "duplicate" : "accepted",
                        status_code: f.duplicate ? 409 : 202,
                        adr: entry,
                        error: null,
                      },
                    ],
                  },
                  202
                );
              }
              await new Promise((resolve) => {
                f.release = resolve;
              });
              f.uploaded = true;
              if (f.uploadError)
                return json(
                  { detail: "Processing failed after persistence" },
                  500
                );
              if (f.partial)
                return json(
                  {
                    repository_id: "repo_a",
                    total: 2,
                    completed: 0,
                    accepted: 1,
                    duplicates: 0,
                    failed: 1,
                    results: [
                      {
                        index: 0,
                        filename: "storage.md",
                        status: "accepted",
                        status_code: 202,
                        adr: { ...doc, status: "PENDING" },
                        error: null,
                      },
                      {
                        index: 1,
                        filename: "invalid.pdf",
                        status: "failed",
                        status_code: 422,
                        adr: null,
                        error: "Document could not be extracted",
                      },
                    ],
                  },
                  207
                );
              return json(
                {
                  repository_id: "repo_a",
                  total: 1,
                  completed: 0,
                  accepted: 1,
                  duplicates: 0,
                  failed: 0,
                  results: [
                    {
                      index: 0,
                      filename: "storage.md",
                      status: "accepted",
                      status_code: 202,
                      adr: { ...doc, status: "PENDING" },
                      error: null,
                    },
                  ],
                },
                202
              );
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
  screenshots = await mkdtemp(
    path.join(tmpdir(), "decisionguard-detail-modal-")
  );
  const token =
    "e30." +
    Buffer.from(
      JSON.stringify({ sub: "alice", exp: Date.now() / 1000 + 3600 })
    ).toString("base64url") +
    ".test";
  await browser.call("Page.addScriptToEvaluateOnNewDocument", {
    source: `localStorage.setItem('token', ${JSON.stringify(token)});`,
  });
});
beforeEach(async () => {
  fixture = setup();
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  });
});
after(async () => {
  fixture.release?.();
  await browser?.close();
  await server?.close();
});
const contains = (text) =>
  `document.body.innerText.includes(${JSON.stringify(text)})`;
async function click(text) {
  await browser.evaluate(
    `Array.from(document.querySelectorAll('button')).find(e=>e.textContent.trim()===${JSON.stringify(text)}).click()`
  );
}
async function visit() {
  await browser.navigate(base + "/projects/repo_a/decisions");
  await browser.waitFor(contains("Use PostgreSQL"));
}
async function selectFile() {
  await browser.evaluate(
    `(() => { const input=document.querySelector('#adr-files'); const dt=new DataTransfer(); dt.items.add(new File(['# Storage'], 'storage.md', {type:'text/markdown'})); input.files=dt.files; input.dispatchEvent(new Event('change',{bubbles:true})); })()`
  );
}

const row = (id) => `document.querySelector('article[aria-label="${id}.md"]')`;
const rowText = (id, text) =>
  `${row(id)}?.innerText.includes(${JSON.stringify(text)})`;
const rowAction = (id, action) =>
  `Array.from(${row(id)}.querySelectorAll('button')).find(b => b.textContent.trim() === '${action}').click()`;
test("delete requires confirmation, prevents duplicate submissions and removes only the confirmed document", async () => {
  fixture.documents = ["001", "002"].map((id) => ({
    ...doc,
    id,
    source_name: `${id}.md`,
  }));
  await visit();
  await click("Source Documents");
  await browser.waitFor(rowText("001", "COMPLETED"));
  await browser.evaluate(rowAction("001", "Delete"));
  await browser.waitFor("!!document.querySelector('[role=dialog]')");
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[role=dialog]').innerText.includes('001.md')"
    ),
    true
  );
  assert.equal(fixture.calls.filter((c) => c.method === "DELETE").length, 0);
  await click("Cancel");
  await browser.waitFor("!document.querySelector('[role=dialog]')");
  assert.equal(await browser.evaluate(`!!${row("001")}`), true);
  await browser.evaluate(rowAction("001", "Delete"));
  fixture.deleteDelay = 300;
  await browser.waitFor("!!document.querySelector('[role=dialog]')");
  await browser.evaluate(
    `(() => {const button=Array.from(document.querySelectorAll('[role=dialog] button')).find(b=>b.textContent.trim()==='Delete ADR'); button.click(); button.click();})()`
  );
  await browser.waitFor(contains("Deleting..."));
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('[role=dialog] button')).every(b => b.disabled)"
    ),
    true
  );
  await browser.waitFor(contains("Deleted 001.md."));
  assert.equal(await browser.evaluate(`!!${row("001")}`), false);
  assert.equal(await browser.evaluate(`!!${row("002")}`), true);
  assert.equal(fixture.calls.filter((c) => c.method === "DELETE").length, 1);
});

test("failed deletion preserves the ADR and shows a useful error", async () => {
  fixture.documents = [{ ...doc, id: "001", source_name: "001.md" }];
  fixture.deleteError = true;
  await visit();
  await click("Source Documents");
  await browser.waitFor(rowText("001", "COMPLETED"));
  await browser.evaluate(rowAction("001", "Delete"));
  await browser.waitFor("!!document.querySelector('[role=dialog]')");
  await click("Delete ADR");
  await browser.waitFor(rowText("001", "Deletion unavailable"));
  assert.equal(await browser.evaluate(`!!${row("001")}`), true);
  assert.equal(fixture.documents.length, 1);
});

test("three ADR identities persist independently across manual status, completion, retry and reload without polling", async () => {
  fixture.documents = ["001", "002", "003"].map((id) => ({
    ...doc,
    id,
    source_name: `${id}.md`,
    status: "PENDING",
  }));
  await visit();
  await click("Source Documents");
  await browser.waitFor(rowText("003", "PENDING"));
  await new Promise((resolve) => setTimeout(resolve, 1100));
  assert.equal(
    fixture.calls.some((call) => call.url.endsWith("/status")),
    false
  );
  fixture.documents[0].status = "PROCESSING";
  fixture.statusDelay = 400;
  await browser.evaluate(rowAction("001", "Check Status"));
  await browser.waitFor(rowText("001", "Checking..."));
  assert.equal(
    await browser.evaluate(`${row("002")}.querySelector('button').disabled`),
    false
  );
  await browser.waitFor(rowText("001", "PROCESSING"));
  assert.equal(await browser.evaluate(rowText("002", "PENDING")), true);
  fixture.statusDelay = 0;
  fixture.documents[0].status = "COMPLETED";
  const before = fixture.calls.filter((c) =>
    c.url.includes("/knowledge?")
  ).length;
  await browser.evaluate(rowAction("001", "Check Status"));
  await browser.waitFor(rowText("001", "COMPLETED"));
  await click("Architectural Knowledge");
  await browser.waitFor(contains("Use PostgreSQL"));
  assert.ok(
    fixture.calls.filter((c) => c.url.includes("/knowledge?")).length > before
  );
  await click("Source Documents");
  fixture.documents[1].status = "FAILED";
  fixture.documents[1].error = "Document processing failed. Please retry.";
  await browser.evaluate(rowAction("002", "Check Status"));
  await browser.waitFor(rowText("002", "Document processing failed"));
  assert.equal(
    await browser.evaluate(`${row("001")}.innerText.includes('Retry')`),
    false
  );
  await browser.evaluate(rowAction("002", "Retry"));
  await browser.waitFor(rowText("002", "PENDING"));
  assert.equal(
    fixture.calls.filter((c) => c.url.endsWith("/002/retry")).length,
    1
  );
  await visit();
  await click("Source Documents");
  await browser.waitFor(rowText("001", "COMPLETED"));
  assert.equal(await browser.evaluate(rowText("002", "PENDING")), true);
  assert.equal(await browser.evaluate(rowText("003", "PENDING")), true);
  assert.equal(
    fixture.calls.filter((c) => c.url.endsWith("/status")).length,
    3
  );
});
for (const code of [403, 404, 409])
  test(`ADR retry ${code} stays scoped and preserves existing status`, async () => {
    fixture.documents = [
      {
        ...doc,
        id: "001",
        source_name: "001.md",
        status: "FAILED",
        error: "Document failed",
      },
    ];
    fixture.retryError = code;
    await visit();
    await click("Source Documents");
    await browser.waitFor(rowText("001", "FAILED"));
    await browser.evaluate(rowAction("001", "Retry"));
    await browser.waitFor(
      `${row("001")}.querySelector('[role=alert]') !== null`
    );
    assert.equal(await browser.evaluate(rowText("001", "FAILED")), true);
    assert.equal(
      fixture.calls.filter((c) => c.url.endsWith("/001/retry")).length,
      1
    );
  });

async function closeDrawer() {
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "Escape",
    code: "Escape",
    windowsVirtualKeyCode: 27,
  });
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "Escape",
    code: "Escape",
    windowsVirtualKeyCode: 27,
  });
  await browser.waitFor("!document.querySelector('[role=dialog]')");
}

async function capture(name) {
  const screenshot = await browser.call("Page.captureScreenshot", {
    format: "png",
  });
  await writeFile(
    path.join(screenshots, name),
    Buffer.from(screenshot.data, "base64")
  );
}

test("desktop modal groups canonical entities, expands large content and restores keyboard focus", async () => {
  fixture.detail = {
    description:
      "Long architectural statement. ".repeat(90) +
      "End of complete statement.",
    entities: [
      {
        id: "orders",
        name: "Orders",
        label: "Service",
        relationship: "AFFECTS",
        source: "ADR",
      },
      {
        id: "orders",
        name: "Orders",
        label: "Service",
        relationship: "CONSTRAINS",
        source: "ADR",
      },
      {
        id: "orders",
        name: "Orders",
        label: "Service",
        relationship: "AFFECTS",
        source: "ADR",
      },
      ...Array.from({ length: 12 }, (_, index) => ({
        id: `entity_${index}`,
        name: `Entity ${index}`,
        label: "Component",
        relationship: "AFFECTS",
        source: "ADR",
      })),
    ],
    evidence: Array.from({ length: 6 }, (_, index) => ({
      id: `chunk_${index}`,
      adr_id: "adr_a",
      section: `Evidence section ${index}`,
      chunk_index: index,
      excerpt:
        `Supporting excerpt ${index}. ` + "Readable evidence. ".repeat(20),
      truncated: index === 5,
      content_hash: `chunk-hash-${index}`,
    })),
  };
  await visit();
  await click("Use PostgreSQL");
  await browser.waitFor(contains("Show full statement"));
  const geometry = await browser.evaluate(
    `(() => { const modal=document.querySelector('[role=dialog]'); const r=modal.getBoundingClientRect(); const grid=document.querySelector('[data-slot=knowledge-detail-columns]'); return { width:r.width, height:r.height, viewport:innerHeight, columns:getComputedStyle(grid).gridTemplateColumns.split(' ').map(parseFloat), named:!!modal.getAttribute('aria-labelledby'), described:!!modal.getAttribute('aria-describedby'), focus:modal.contains(document.activeElement) }; })()`
  );
  assert.ok(Math.abs(geometry.width - 900) < 2);
  assert.ok(geometry.height <= geometry.viewport * 0.9 + 1);
  assert.ok(
    geometry.columns[0] / geometry.columns[1] > 1.4 &&
      geometry.columns[0] / geometry.columns[1] < 1.6
  );
  assert.equal(geometry.named && geometry.described && geometry.focus, true);
  assert.deepEqual(
    await browser.evaluate(
      `Array.from(document.querySelector('[data-entity-id="orders"]').querySelectorAll('span')).map(e=>e.textContent)`
    ),
    ["Affected", "Constrained"]
  );
  assert.equal(
    await browser.evaluate(
      `document.querySelectorAll('[data-entity-id="orders"]').length`
    ),
    1
  );
  assert.equal(
    await browser.evaluate(
      `document.querySelectorAll('[data-entity-id]').length`
    ),
    8
  );
  await click("Show all 13 entities");
  assert.equal(
    await browser.evaluate(
      `document.querySelectorAll('[data-entity-id]').length`
    ),
    13
  );
  await click("Show full statement");
  await browser.waitFor(contains("End of complete statement."));
  assert.equal(
    await browser.evaluate(
      `document.querySelector('[data-slot=knowledge-detail-scroll]').scrollHeight > document.querySelector('[data-slot=knowledge-detail-scroll]').clientHeight`
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      `Array.from(document.querySelectorAll('[role=dialog] details')).find(d=>d.querySelector('summary').textContent.includes('Evidence section 5')).open`
    ),
    false
  );
  await browser.evaluate(
    `Array.from(document.querySelectorAll('[role=dialog] summary')).find(e=>e.textContent.includes('Evidence section 5')).click()`
  );
  await browser.waitFor(contains("Supporting excerpt 5."));
  await capture("desktop-detail.png");
  for (let i = 0; i < 20; i++) {
    await browser.call("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
    });
    await browser.call("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
    });
    await browser.waitFor(
      `document.querySelector('[role=dialog]').contains(document.activeElement)`
    );
  }
  await closeDrawer();
  assert.equal(
    await browser.evaluate(`document.activeElement.textContent.trim()`),
    "Use PostgreSQL"
  );
});

test("mobile modal is full screen, stacks sections and keeps long references inside the viewport", async () => {
  fixture.detail = {
    sources: [
      {
        ...source,
        source_name: "a".repeat(180) + ".md",
        content_hash: "f".repeat(160),
      },
    ],
    description: "Mobile architectural statement. ".repeat(60),
  };
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  await visit();
  await click("Use PostgreSQL");
  await browser.waitFor(contains("Show full statement"));
  const geometry = await browser.evaluate(
    `(() => { const modal=document.querySelector('[role=dialog]'), r=modal.getBoundingClientRect(), grid=document.querySelector('[data-slot=knowledge-detail-columns]'), scroll=document.querySelector('[data-slot=knowledge-detail-scroll]'); return { width:r.width,height:r.height,left:r.left,top:r.top,viewportWidth:innerWidth,viewportHeight:innerHeight,columns:getComputedStyle(grid).gridTemplateColumns.split(' ').length,overflow:scroll.scrollWidth>scroll.clientWidth,scrollable:scroll.scrollHeight>scroll.clientHeight }; })()`
  );
  assert.equal(geometry.width, geometry.viewportWidth);
  assert.equal(geometry.height, geometry.viewportHeight);
  assert.equal(geometry.left, 0);
  assert.equal(geometry.top, 0);
  assert.equal(geometry.columns, 1);
  assert.equal(geometry.overflow, false);
  assert.equal(geometry.scrollable, true);
  await capture("mobile-detail.png");
  await click("Close architectural knowledge");
  await browser.waitFor("!document.querySelector('[role=dialog]')");
  console.log("Modal visual checks:", screenshots);
});

test("detail modal preserves API errors and retries without showing stale evidence", async () => {
  fixture.detailError = true;
  await visit();
  await click("Use PostgreSQL");
  await browser.waitFor(contains("Evidence temporarily unavailable"));
  assert.equal(
    await browser.evaluate(
      contains("PostgreSQL provides transactional persistence.")
    ),
    false
  );
  fixture.detailError = false;
  await click("Try again");
  await browser.waitFor(
    contains("PostgreSQL provides transactional persistence.")
  );
  assert.equal(
    fixture.calls.filter((c) => c.url.endsWith("/knowledge/decision_a")).length,
    2
  );
  await closeDrawer();
});

const knowledgeCalls = () =>
  fixture.calls.filter((c) => c.url.includes("/knowledge?")).length;

test("collaborator can open a constraint modal with its actual statement and relationships", async () => {
  fixture.owner = false;
  await visit();
  await click("Encrypt order data.");
  await browser.waitFor(
    "document.querySelector('[role=dialog]')?.innerText.includes('Constrained')"
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[role=dialog] h2')?.textContent"
    ),
    "Architectural constraint"
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('#knowledge-complete-statement')?.textContent"
    ),
    "Encrypt order data."
  );
  assert.equal(
    fixture.calls.some((c) => c.url.endsWith("/adrs")),
    false
  );
  await closeDrawer();
});

test("knowledge stays cached across navigation while ADR list remount reads persisted status", async () => {
  await visit();
  assert.equal(knowledgeCalls(), 1);
  await click("Decisions");
  await browser.waitFor(contains("Use PostgreSQL"));
  await browser.waitFor(
    "!document.querySelector('main').innerText.includes('Encrypt order data.')"
  );
  assert.equal(knowledgeCalls(), 2);
  await click("Constraints");
  await browser.waitFor(contains("Encrypt order data."));
  await browser.waitFor(
    "!document.querySelector('main').innerText.includes('Use PostgreSQL')"
  );
  assert.equal(knowledgeCalls(), 3);
  await click("All");
  await browser.waitFor(contains("Use PostgreSQL"));
  await click("Decisions");
  await browser.waitFor(contains("Use PostgreSQL"));
  await browser.waitFor(
    "!document.querySelector('main').innerText.includes('Encrypt order data.')"
  );
  assert.equal(knowledgeCalls(), 3);
  await click("Use PostgreSQL");
  await browser.waitFor(
    contains("PostgreSQL provides transactional persistence.")
  );
  await closeDrawer();
  await click("Use PostgreSQL");
  await browser.waitFor(
    contains("PostgreSQL provides transactional persistence.")
  );
  await closeDrawer();
  assert.equal(
    fixture.calls.filter((c) => c.url.endsWith("/knowledge/decision_a")).length,
    1
  );
  await click("Source Documents");
  await browser.waitFor(
    contains("No ADR source documents have been uploaded.")
  );
  await click("Architectural Knowledge");
  await browser.waitFor(contains("Use PostgreSQL"));
  await click("Source Documents");
  await browser.waitFor(
    contains("No ADR source documents have been uploaded.")
  );
  await browser.waitFor(
    "!Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Refresh documents').disabled"
  );
  assert.equal(fixture.calls.filter((c) => c.url.endsWith("/adrs")).length, 2);
  await browser.evaluate(
    "document.querySelector('a[href=\"/projects/repo_a/overview\"]').click()"
  );
  await browser.waitFor(contains("Architecture Overview"));
  await browser.evaluate(
    "document.querySelector('a[href=\"/projects/repo_a/decisions\"]').click()"
  );
  await browser.waitFor(contains("Use PostgreSQL"));
  assert.equal(knowledgeCalls(), 3);
  await click("Refresh knowledge");
  await browser.waitFor(
    "!Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='Refresh knowledge').disabled"
  );
  assert.equal(knowledgeCalls(), 4);
  await click("Decisions");
  await browser.waitFor(contains("Use PostgreSQL"));
  await browser.waitFor(
    "!document.querySelector('main').innerText.includes('Encrypt order data.')"
  );
  assert.equal(knowledgeCalls(), 5);
});

test("default knowledge tab supports filtering, search and evidence drawer", async () => {
  await visit();
  assert.equal(
    await browser.evaluate(
      "document.querySelector('#knowledge-tab').getAttribute('aria-selected')"
    ),
    "true"
  );
  await click("Use PostgreSQL");
  await browser.waitFor(
    contains("PostgreSQL provides transactional persistence.")
  );
  for (const label of [
    "Orders",
    "Provenance",
    "adr-hash",
    "chunk-hash",
    "Complete statement",
  ])
    assert.equal(await browser.evaluate(contains(label)), true);
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "Escape",
    code: "Escape",
    windowsVirtualKeyCode: 27,
  });
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "Escape",
    code: "Escape",
    windowsVirtualKeyCode: 27,
  });
  await browser.waitFor("!document.querySelector('[role=dialog]')");
  await click("Constraints");
  await browser.waitFor(contains("Encrypt order data."));
  await browser.waitFor(
    "!document.querySelector('main').innerText.includes('Use PostgreSQL')"
  );
  await browser.evaluate(
    `(() => { const input=document.querySelector('#knowledge-search'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'missing'); input.dispatchEvent(new Event('input',{bubbles:true})); })()`
  );
  await click("Search");
  await browser.waitFor(contains("No matching architectural knowledge"));
  assert.ok(fixture.calls.some((c) => c.url.includes("search=missing")));
});

test("knowledge inventory replaces the risk panel and exact positive badges remain truthful with truncated associations", async () => {
  fixture.items = [
    decision,
    constraint,
    { ...decision, id: "decision_b", title: "Use PostgreSQL for reporting" },
    ...Array.from({ length: 59 }, (_, index) => ({
      ...decision,
      id: `neutral_${index}`,
      title: `Stored decision ${index + 1}`,
    })),
  ];
  const risk = (identity, classification, kind = "DECISION") => ({
    identity,
    classification,
    kind,
    pr_numbers: [2],
    finding_ids: ["finding"],
    adr_ids: ["adr_a"],
  });
  fixture.attention = {
    availability: { status: "available", reason: null },
    total: 100,
    items: [
      risk("decision_a", "CONFIRMED"),
      risk("decision_b", "POTENTIAL"),
      risk("constraint_a", "CONFIRMED", "REQUIREMENT"),
      risk("neutral_0", "CONFIRMED", "REQUIREMENT"),
    ],
  };
  await visit();
  assert.equal(
    await browser.evaluate(contains("Current PR decision risks")),
    false
  );
  assert.equal(
    await browser.evaluate(contains("Technical association identities")),
    false
  );
  assert.equal(
    await browser.evaluate("document.querySelectorAll('main article').length"),
    20
  );
  const cards = await browser.evaluate(
    "Array.from(document.querySelectorAll('main article')).map(e=>e.innerText)"
  );
  assert.match(cards[0], /Confirmed conflict/);
  assert.match(cards[2], /Potential risk/);
  for (const card of [cards[1], ...cards.slice(3)])
    assert.doesNotMatch(
      card,
      /Confirmed conflict|Potential risk|No currently observed PR risk|risk unavailable|displayed risk subset/
    );
  assert.equal(knowledgeCalls(), 1);
  assert.equal(
    fixture.calls.some((call) => /evaluations|pull-requests/.test(call.url)),
    false
  );
  for (const width of [390, 320]) {
    await browser.call("Emulation.setDeviceMetricsOverride", {
      width,
      height: 844,
      deviceScaleFactor: 1,
      mobile: true,
    });
    assert.equal(
      await browser.evaluate(
        "document.documentElement.scrollWidth <= innerWidth"
      ),
      true
    );
  }
  await click("Next");
  await browser.waitFor(contains("Showing 21–40 of 62 entries"));
  assert.ok(fixture.calls.some((call) => call.url.includes("offset=20")));
  await click("Constraints");
  await browser.waitFor(contains("Showing 1–1 of 1 entries"));
  assert.equal(
    await browser.evaluate("document.querySelectorAll('main article').length"),
    1
  );
  await click("Decisions");
  await browser.waitFor(contains("Showing 1–20 of 61 entries"));
  await click("All");
  await browser.waitFor(contains("Showing 1–20 of 62 entries"));
  assert.ok(fixture.calls.every((call) => call.method === "GET"));
});

test("stable-ID modal links preserve back/forward navigation and open records outside the loaded knowledge page", async () => {
  await visit();
  await click("Use PostgreSQL");
  await browser.waitFor(
    contains("PostgreSQL provides transactional persistence.")
  );
  assert.equal(
    await browser.evaluate(
      "new URLSearchParams(location.search).get('record')"
    ),
    "decision_a"
  );
  await closeDrawer();
  assert.equal(
    await browser.evaluate(
      "new URLSearchParams(location.search).has('record')"
    ),
    false
  );
  await browser.evaluate("history.back()");
  await browser.waitFor(
    "document.querySelector('[data-slot=knowledge-detail-modal]') !== null"
  );
  await browser.evaluate("history.back()");
  await browser.waitFor(
    "!document.querySelector('[data-slot=knowledge-detail-modal]')"
  );
  await browser.evaluate("history.forward()");
  await browser.waitFor(
    "document.querySelector('[data-slot=knowledge-detail-modal]') !== null"
  );
  assert.equal(
    fixture.calls.filter((call) => call.url.endsWith("/knowledge/decision_a"))
      .length,
    1
  );
  fixture.items = [constraint];
  await browser.navigate(
    base + "/projects/repo_a/decisions?record=decision_a&origin=overview"
  );
  await browser.waitFor(
    contains("PostgreSQL provides transactional persistence.")
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('main article h2').length"
    ),
    1
  );
  await closeDrawer();
  assert.equal(
    await browser.evaluate(
      "new URLSearchParams(location.search).get('origin')"
    ),
    "overview"
  );
  assert.equal(
    await browser.evaluate(
      "new URLSearchParams(location.search).has('record')"
    ),
    false
  );
});

test("knowledge loading, empty, unavailable risk, missing record and repository denial remain honest", async () => {
  fixture.knowledgeDelay = 600;
  fixture.items = [];
  await browser.navigate(base + "/projects/repo_a/decisions");
  await browser.waitFor(contains("Loading architectural knowledge"));
  await browser.waitFor(contains("No extracted architectural knowledge yet"));
  fixture.knowledgeDelay = 0;
  fixture.knowledgeError = 503;
  await browser.navigate(base + "/projects/repo_a/decisions");
  await browser.waitFor(contains("Knowledge temporarily unavailable"));
  fixture.knowledgeError = null;
  fixture.items = [decision, constraint];
  await browser.click("Try again");
  await browser.waitFor(contains("Use PostgreSQL"));
  assert.equal(await browser.evaluate(contains("PR risk unavailable")), false);
  assert.equal(
    await browser.evaluate(contains("No currently observed PR risk")),
    false
  );
  await browser.navigate(base + "/projects/repo_a/decisions?record=missing");
  await browser.waitFor(
    "document.querySelector('[role=dialog] [role=alert]') !== null"
  );
  assert.equal(
    await browser.evaluate(
      contains("PostgreSQL provides transactional persistence.")
    ),
    false
  );
  await browser.navigate(
    base + "/projects/repo_other/decisions?record=decision_a"
  );
  await browser.waitFor(contains("Repository access denied"));
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[data-slot=knowledge-detail-modal]') === null"
    ),
    true
  );
});

test("collaborator source tab is hidden and revocation hides repository content", async () => {
  fixture.owner = false;
  await visit();
  assert.equal(
    await browser.evaluate("!!document.querySelector('#sources-tab')"),
    false
  );
  assert.equal(
    fixture.calls.some((c) => c.url.endsWith("/adrs")),
    false
  );
  fixture.denied = true;
  await browser.navigate(base + "/projects/repo_a/decisions");
  await browser.waitFor(contains("Repository access denied"));
  assert.equal(
    await browser.evaluate(contains("Persist orders in PostgreSQL.")),
    false
  );
});

for (const error of [false, true, "partial"])
  test(`asynchronous upload locks submissions and refreshes both inventories (${error ? "failure" : "success"})`, async () => {
    fixture.uploadError = error === true;
    fixture.partial = error === "partial";
    await visit();
    await click("Source Documents");
    await browser.waitFor(
      contains("No ADR source documents have been uploaded.")
    );
    await selectFile();
    await click("Upload documents");
    await browser.waitFor(contains("Uploading documents..."));
    assert.equal(
      await browser.evaluate("document.querySelector('#adr-files').disabled"),
      true
    );
    assert.equal(
      await browser.evaluate(
        "document.querySelector('#knowledge-tab').disabled"
      ),
      true
    );
    assert.equal(
      await browser.evaluate(
        "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.includes('Uploading documents')).disabled"
      ),
      true
    );
    await browser.evaluate(
      "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.includes('Uploading documents')).click()"
    );
    assert.equal(fixture.calls.filter((c) => c.method === "POST").length, 1);
    assert.match(
      fixture.calls.find((c) => c.method === "POST").type,
      /^multipart\/form-data; boundary=/
    );
    const knowledgeCalls = fixture.calls.filter((c) =>
      c.url.includes("/knowledge?")
    ).length;
    fixture.release();
    await browser.waitFor(
      contains(
        error === true ? "Some data may have been persisted" : "1 accepted"
      )
    );
    await browser.waitFor(contains("storage.md"));
    if (error === "partial")
      assert.equal(
        await browser.evaluate(contains("Document could not be extracted")),
        true
      );
    assert.ok(
      fixture.calls.filter((c) => c.url.endsWith("/adrs") && c.method === "GET")
        .length >= 2
    );
    await click("Architectural Knowledge");
    await browser.waitFor(contains("Use PostgreSQL"));
    assert.ok(
      fixture.calls.filter((c) => c.url.includes("/knowledge?")).length >
        knowledgeCalls
    );
  });

test("three sequential accepted uploads preserve individual ADR IDs and duplicate response adds no row", async () => {
  fixture.sequential = true;
  await visit();
  await click("Source Documents");
  await browser.waitFor(contains("No ADR source documents have been uploaded"));
  for (let index = 1; index <= 3; index++) {
    await selectFile();
    await click("Upload documents");
    await browser.waitFor(
      `document.querySelector('article[aria-label="adr-${index}.md"]') !== null`
    );
    await browser.waitFor("!document.querySelector('#adr-files').disabled");
    assert.equal(
      await browser.evaluate("document.querySelectorAll('article').length"),
      index
    );
    assert.equal(await browser.evaluate(contains("1 accepted")), true);
  }
  fixture.duplicate = true;
  await selectFile();
  await click("Upload documents");
  await browser.waitFor(contains("1 duplicate"));
  assert.equal(
    await browser.evaluate("document.querySelectorAll('article').length"),
    3
  );
  assert.equal(
    fixture.calls.filter((c) => c.url.endsWith("/status")).length,
    0
  );
  assert.deepEqual(
    fixture.documents.map((d) => d.id),
    ["adr_1", "adr_2", "adr_3"]
  );
});
