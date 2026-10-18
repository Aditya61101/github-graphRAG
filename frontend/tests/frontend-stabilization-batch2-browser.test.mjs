import assert from "node:assert/strict";
import { before, after, beforeEach, test } from "node:test";
import { createServer } from "vite";
import { openBrowser } from "./browser-helper.mjs";

let server, browser, base, fixture;
const prefix = "decisionguard:adr-guidance:v1:";
const completion = {
  onboarding_completed: true,
  onboarding_completed_at: "2026-10-18T00:00:00Z",
  next: "/projects",
};
const token =
  "e30." +
  Buffer.from(
    JSON.stringify({ sub: "usr_alice", exp: Date.now() / 1000 + 3600 })
  ).toString("base64url") +
  ".test";
const availability = { status: "available", reason: null };
const document = {
  id: "adr_a",
  title: "Storage",
  source_name: "storage.md",
  source_type: "MANUAL_UPLOAD",
  created_at: "2026-10-17T00:00:00Z",
  updated_at: "2026-10-18T00:00:00Z",
  status: "COMPLETED",
  error: null,
};
const decision = {
  id: "decision_a",
  kind: "decision",
  title: "Use PostgreSQL",
  description: "Keep transactional storage.",
  type: "technology",
  sources: [],
};
function setup() {
  return {
    user: { id: "usr_alice", username: "alice", email: null, avatar_url: null },
    reconnect: false,
    manage: true,
    admin: true,
    status: "COMPLETED",
    docs: [],
    docsError: false,
    docsDelay: 0,
    ready: true,
    calls: [],
    pageFiles: [],
    profileDelay: 0,
    profileFailure: false,
  };
}
before(async () => {
  fixture = setup();
  server = await createServer({
    server: { host: "127.0.0.1", port: 0, hmr: false },
    define: {
      "import.meta.env.VITE_BACKEND_URL": JSON.stringify("/__batch2_api"),
    },
    plugins: [
      {
        name: "batch2-fixtures",
        configureServer(vite) {
          vite.middlewares.use(async (req, res, next) => {
            const pageFile = new URL(req.url, "http://test").pathname;
            if (pageFile.startsWith("/src/pages/"))
              fixture.pageFiles.push(pageFile);
            if (pageFile === "/src/pages/settings.tsx") {
              if (fixture.profileDelay)
                await new Promise((resolve) =>
                  setTimeout(resolve, fixture.profileDelay)
                );
              if (fixture.profileFailure) {
                res.statusCode = 503;
                res.setHeader("Content-Type", "text/javascript");
                return res.end("Page asset unavailable");
              }
            }
            if (req.url === "/__batch2_reset") {
              res.setHeader("Content-Type", "text/html");
              return res.end("<p>Fixture reset</p>");
            }
            if (req.url === "/__avatar.svg") {
              res.setHeader("Content-Type", "image/svg+xml");
              return res.end(
                '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#227755"/></svg>'
              );
            }
            next();
          });
          vite.middlewares.use("/__batch2_api", async (req, res) => {
            const f = fixture;
            const url = new URL(req.url, "http://test");
            f.calls.push({ path: url.pathname, method: req.method });
            res.setHeader("Content-Type", "application/json");
            const json = (value, status = 200) => {
              res.statusCode = status;
              res.end(JSON.stringify(value));
            };
            if (url.pathname === "/auth/me")
              return json({
                ...completion,
                user: f.user,
                reconnect_required: f.reconnect,
                installations: [],
                installation_onboarding_required: false,
                installation_discovery_status: "not_requested",
              });
            if (url.pathname === "/auth/onboarding")
              return json({
                ...completion,
                eligible: false,
                screen: "completed",
                installations: [],
                installation_discovery_status: "complete",
                installation_outcome: null,
              });
            if (url.pathname === "/repositories") return json([]);
            const repoId = url.pathname.split("/")[2];
            if (url.pathname.endsWith("/overview")) {
              if (!f.ready)
                return json(
                  {
                    detail:
                      "Repository graph update or repair is in progress; retry later",
                  },
                  503
                );
              return json({
                repository: {
                  id: repoId,
                  owner: "alice",
                  name: repoId,
                  full_name: `alice/${repoId}`,
                  repository_url: `https://github.com/alice/${repoId}`,
                  tracked_branch: "main",
                  status: f.status,
                  indexed_commit_sha: "abcdef123",
                },
                permissions: {
                  can_manage_source_documents: f.manage,
                  can_view_settings: f.admin,
                },
                kpis: Object.fromEntries(
                  [
                    "architecture_decisions",
                    "decisions_at_risk",
                    "prs_analyzed",
                    "architectural_warnings",
                  ].map((key) => [key, { value: 0, availability }])
                ),
                decisions_needing_attention: {
                  availability,
                  items: [],
                  total: 0,
                },
                recent_pr_reviews: { availability, items: [] },
                review_activity: { availability, items: [], window_days: 7 },
              });
            }
            if (url.pathname.endsWith("/adrs")) {
              if (!f.manage) return json({ detail: "Owner only" }, 403);
              const snapshot = structuredClone(f.docs);
              if (f.docsDelay)
                await new Promise((r) => setTimeout(r, f.docsDelay));
              return f.docsError
                ? json({ detail: "Inventory unavailable" }, 503)
                : json(snapshot);
            }
            if (
              url.pathname.endsWith("/adrs/adr_a") &&
              req.method === "DELETE"
            ) {
              if (!f.admin)
                return json({ detail: "Administrator required" }, 403);
              f.docs = [];
              f.ready = false;
              return json({ adr_id: "adr_a", status: "DELETED" }, 202);
            }
            if (url.pathname.endsWith("/knowledge"))
              return json({
                items: [decision],
                total: 1,
                offset: 0,
                limit: 20,
              });
            if (url.pathname.endsWith("/knowledge/decision_a"))
              return json({
                ...decision,
                entities: [],
                evidence: [],
                entities_truncated: false,
                evidence_truncated: false,
                relationship_basis: "documented_ADR_knowledge",
              });
            if (url.pathname.endsWith("/graph"))
              return json({ nodes: [], edges: [] });
            if (url.pathname.endsWith("/pull-requests"))
              return json({ items: [], total: 0, offset: 0, limit: 20 });
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
    source: `
    localStorage.setItem('token', ${JSON.stringify(token)});
    const blocked = localStorage.getItem('batch2-storage-blocked') === '1';
    const originalGet = Storage.prototype.getItem, originalSet = Storage.prototype.setItem;
    window.__guidanceWrites = 0;
    Storage.prototype.getItem = function(key) {
      if (blocked && key.startsWith(${JSON.stringify(prefix)})) throw new Error('Storage blocked');
      return originalGet.call(this, key);
    };
    Storage.prototype.setItem = function(key, value) {
      if (key.startsWith(${JSON.stringify(prefix)})) {
        window.__guidanceWrites++;
        if (blocked) throw new Error('Storage blocked');
      }
      return originalSet.call(this, key, value);
    };
  `,
  });
});
beforeEach(async () => {
  fixture = setup();
  await browser.navigate(base + "/__batch2_reset");
  await browser.evaluate(
    `Object.keys(localStorage).filter(k => k.startsWith(${JSON.stringify(prefix)})).forEach(k => localStorage.removeItem(k)); localStorage.removeItem('batch2-storage-blocked');`
  );
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  });
});
after(async () => {
  await browser?.close();
  await server?.close();
});
const text = (value) =>
  `document.body.innerText.includes(${JSON.stringify(value)})`;
const banner = "!!document.querySelector('#adr-guidance-title')";
const at = (path) => `location.pathname === ${JSON.stringify(path)}`;
const inventories = () =>
  fixture.calls.filter((call) => call.path.endsWith("/adrs"));
async function visit(path = "/projects/repo_a/overview") {
  await browser.navigate(base + path);
  await browser.waitFor(
    path === "/settings"
      ? "Array.from(document.querySelectorAll('h1')).some(h => h.textContent.trim() === 'My Profile')"
      : text("Architecture Overview")
  );
}
async function link(label) {
  const expr = `Array.from(document.querySelectorAll('a')).find(a => a.textContent.trim() === ${JSON.stringify(label)} || a.getAttribute('aria-label') === ${JSON.stringify(label)})`;
  await browser.waitFor(`!!${expr}`);
  await browser.evaluate(`${expr}.click()`);
}

test("profile uses actual identity, avatar, email and working links without placeholders or unfinished apps", async () => {
  fixture.user.email = "alice@example.test";
  fixture.user.avatar_url = base + "/__avatar.svg";
  await visit("/settings");
  await browser.waitFor(
    `document.querySelector('[aria-label="GitHub profile"] img')?.getAttribute('alt') === ${JSON.stringify("alice's GitHub avatar")}`
  );
  assert.equal(await browser.evaluate(text("alice@example.test")), true);
  assert.equal(
    await browser.evaluate(
      "document.querySelector('a[href=\"https://github.com/alice\"]').target"
    ),
    "_blank"
  );
  for (const label of [
    "Administrator",
    "Indexed entities",
    "82K",
    "Save changes",
    "Cancel",
    "Google Drive",
    "Confluence",
    "Connected apps",
    "Full name",
    "Display name",
  ]) {
    assert.equal(await browser.evaluate(text(label)), false, label);
  }
  assert.equal(
    await browser.evaluate("document.querySelectorAll('main input').length"),
    0
  );
  await browser.evaluate("window.__spaSentinel = 'retained'");
  await link("Manage repository access");
  await browser.waitFor(at("/install"));
  await browser.waitFor(text("Manage GitHub repository access"));
  assert.equal(await browser.evaluate("window.__spaSentinel"), "retained");
  assert.equal(fixture.calls.filter((c) => c.path === "/auth/me").length, 1);
});

test("missing identity fields and reconnect status remain truthful; profile logout works", async () => {
  fixture.reconnect = true;
  await visit("/settings");
  await browser.waitFor(text("Not provided by GitHub"));
  assert.equal(await browser.evaluate(text("Reconnection required")), true);
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === 'Reconnect GitHub')"
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      "!!document.querySelector('[aria-label=\"GitHub profile\"] [data-slot=avatar-fallback]')"
    ),
    true
  );
  await browser.click("Log out");
  await browser.waitFor(at("/auth/login"));
  assert.equal(await browser.evaluate("localStorage.getItem('token')"), null);
});

test("profile breadcrumbs are client-side, unique and return to the dashboard without losing the session", async () => {
  await visit("/settings");
  await browser.evaluate("window.__spaSentinel = 123");
  assert.deepEqual(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('[data-slot=breadcrumb-item]')).map(e => e.textContent.trim())"
    ),
    ["All Repositories", "My Profile"]
  );
  const authCalls = fixture.calls.filter((c) => c.path === "/auth/me").length;
  await browser.evaluate(
    "document.querySelector('[data-slot=breadcrumb-link]').click()"
  );
  await browser.waitFor(at("/projects"));
  await browser.waitFor(
    "!!document.querySelector('[aria-label=\"Open profile menu\"]')"
  );
  assert.equal(await browser.evaluate("window.__spaSentinel"), 123);
  await browser.evaluate(
    "document.querySelector('[aria-label=\"Open profile menu\"]').click()"
  );
  await browser.waitFor(text("My Profile"));
  await link("My Profile");
  await browser.waitFor(at("/settings"));
  assert.equal(await browser.evaluate("window.__spaSentinel"), 123);
  assert.equal(
    fixture.calls.filter((c) => c.path === "/auth/me").length,
    authCalls
  );
});

test("all four repository routes remain available and Settings bookmark redirects with replacement", async () => {
  fixture.manage = false;
  fixture.admin = false;
  await browser.navigate(base + "/projects");
  await browser.waitFor(text("Your repositories"));
  await browser.navigate(base + "/projects/repo_a/settings");
  await browser.waitFor(at("/projects/repo_a/overview"));
  await browser.waitFor(text("Architecture Overview"));
  assert.equal(
    await browser.evaluate(
      "!!document.querySelector('a[aria-label=Settings]')"
    ),
    false
  );
  await browser.evaluate("history.back()");
  await browser.waitFor(at("/projects"));
  await visit();
  for (const [label, expected] of [
    ["Knowledge Explorer", "Knowledge Explorer"],
    ["Decisions", "Architectural Knowledge"],
    ["Pull Requests", "No observed pull requests"],
    ["Overview", "Architecture Overview"],
  ]) {
    await link(label);
    await browser.waitFor(text(expected));
  }
  await browser.navigate(base + "/projects/not-canonical/settings");
  await browser.waitFor(text("Invalid repository ID"));
  assert.equal(
    await browser.evaluate(at("/projects/not-canonical/settings")),
    true
  );
});

test("eligible empty inventory presents once and persists immediately across dismissal, remount and reload", async () => {
  fixture.docsDelay = 350;
  await visit();
  assert.equal(await browser.evaluate(banner), false);
  assert.equal(
    await browser.evaluate(`localStorage.getItem('${prefix}usr_alice:repo_a')`),
    null
  );
  await browser.waitFor(banner);
  assert.equal(
    await browser.evaluate(`localStorage.getItem('${prefix}usr_alice:repo_a')`),
    "shown"
  );
  assert.equal(await browser.evaluate("window.__guidanceWrites"), 1);
  assert.equal(inventories().length, 1);
  await browser.evaluate(
    "document.querySelector('[aria-label=\"Dismiss ADR upload guidance\"]').click()"
  );
  await browser.waitFor(`!(${banner})`);
  await link("Decisions");
  await browser.waitFor(text("Architectural Knowledge"));
  await link("Overview");
  await browser.waitFor(text("Architecture Overview"));
  assert.equal(await browser.evaluate(banner), false);
  await visit();
  assert.equal(await browser.evaluate(banner), false);
  assert.equal(
    inventories().length,
    1,
    "recorded hint must not trigger further eligibility reads"
  );
});

test("guidance CTA opens Sources, focuses upload, and Back/Forward preserves URL-driven tabs", async () => {
  await visit();
  await browser.waitFor(banner);
  await link("Upload an ADR");
  await browser.waitFor(
    "location.search === '?tab=sources' && !!document.querySelector('#adr-files')"
  );
  await browser.waitFor("document.activeElement?.id === 'adr-files'");
  assert.equal(
    await browser.evaluate(
      "document.querySelector('#sources-tab').getAttribute('aria-selected')"
    ),
    "true"
  );
  await browser.click("Architectural Knowledge");
  await browser.waitFor(
    "!location.search.includes('tab=sources') && !!document.querySelector('#knowledge-panel')"
  );
  await browser.evaluate("history.back()");
  await browser.waitFor(
    "location.search.includes('tab=sources') && !!document.querySelector('#adr-files')"
  );
  await browser.evaluate("history.forward()");
  await browser.waitFor("!!document.querySelector('#knowledge-panel')");
  await link("Overview");
  await browser.waitFor(text("Architecture Overview"));
  assert.equal(
    await browser.evaluate(banner),
    false,
    "presentation itself, without dismissal, suppresses the next visit"
  );
  await browser.navigate(
    base + "/projects/repo_a/decisions?tab=sources&record=decision_a"
  );
  await browser.waitFor(
    "!!document.querySelector('[role=dialog]') && !!document.querySelector('#adr-files')"
  );
  await browser.waitFor(
    "document.querySelector('[role=dialog]').contains(document.activeElement)"
  );
  await browser.evaluate(
    "Array.from(document.querySelectorAll('[role=dialog] button')).find(b => b.textContent.trim() === 'Close architectural knowledge').click()"
  );
  await browser.waitFor(
    "!document.querySelector('[role=dialog]') && location.search === '?tab=sources'"
  );
  await browser.waitFor("document.activeElement?.id === 'adr-files'");
});

test("pending, processing, failed and unknown indexing never request owner inventory or claim guidance", async () => {
  for (const status of [
    "PENDING",
    "INDEXING",
    "PROCESSING",
    "FAILED",
    "UNKNOWN",
  ]) {
    fixture.status = status;
    await visit();
    assert.equal(await browser.evaluate(banner), false, status);
  }
  assert.equal(inventories().length, 0);
  assert.equal(
    await browser.evaluate(`localStorage.getItem('${prefix}usr_alice:repo_a')`),
    null
  );
});

test("any active ADR status suppresses guidance and inventory errors are not empty success", async () => {
  for (const status of ["PENDING", "PROCESSING", "COMPLETED", "FAILED"]) {
    fixture.docs = [{ ...document, status }];
    const previous = inventories().length;
    await visit();
    await browser.waitFor(
      "document.querySelector('main h1')?.textContent === 'Architecture Overview'"
    );
    // Let the mocked request and query observer settle before checking absence.
    await new Promise((r) => setTimeout(r, 150));
    assert.equal(inventories().length, previous + 1);
    assert.equal(await browser.evaluate(banner), false, status);
  }
  fixture.docs = [];
  fixture.docsError = true;
  await visit();
  await new Promise((r) => setTimeout(r, 150));
  assert.equal(await browser.evaluate(banner), false);
  assert.equal(
    await browser.evaluate(`localStorage.getItem('${prefix}usr_alice:repo_a')`),
    null
  );
});

test("collaborators do not request owner inventory, including source-tab deep links with a record", async () => {
  fixture.manage = false;
  await visit();
  assert.equal(inventories().length, 0);
  assert.equal(await browser.evaluate(banner), false);
  await browser.navigate(
    base + "/projects/repo_a/decisions?tab=sources&record=decision_a"
  );
  await browser.waitFor(
    text("Documented ADR knowledge and supporting provenance.")
  );
  assert.equal(
    await browser.evaluate("!!document.querySelector('#adr-files')"),
    false
  );
  assert.equal(
    await browser.evaluate("!!document.querySelector('#sources-tab')"),
    false
  );
  assert.equal(inventories().length, 0);
});

test("guidance markers are independent across authenticated users and canonical repositories", async () => {
  await visit();
  await browser.waitFor(banner);
  await visit("/projects/repo_b/overview");
  await browser.waitFor(banner);
  fixture.user = { ...fixture.user, id: "usr_bob", username: "bob" };
  await visit();
  await browser.waitFor(banner);
  assert.deepEqual(
    await browser.evaluate(
      `Object.keys(localStorage).filter(k => k.startsWith('${prefix}')).sort()`
    ),
    [
      `${prefix}usr_alice:repo_a`,
      `${prefix}usr_alice:repo_b`,
      `${prefix}usr_bob:repo_a`,
    ]
  );
});

test("blocked localStorage does not break Overview or repeatedly present within the app session", async () => {
  await browser.evaluate("localStorage.setItem('batch2-storage-blocked', '1')");
  await visit();
  await browser.waitFor(banner);
  assert.equal(await browser.evaluate(text("Architecture Overview")), true);
  assert.equal(await browser.evaluate("window.__guidanceWrites"), 1);
  await link("Decisions");
  await browser.waitFor(text("Architectural Knowledge"));
  await link("Overview");
  await browser.waitFor(text("Architecture Overview"));
  assert.equal(await browser.evaluate(banner), false);
  assert.equal(inventories().length, 1);
});

test("deletion cancels stale inventory and suppresses first-time guidance after readiness recovery", async () => {
  fixture.docs = [document];
  await visit();
  await new Promise((r) => setTimeout(r, 150));
  assert.equal(await browser.evaluate(banner), false);
  await link("Decisions");
  await browser.waitFor(text("Architectural Knowledge"));
  await browser.click("Source Documents");
  await browser.waitFor(text("storage.md"));
  fixture.docsDelay = 500;
  await browser.click("Refresh documents");
  await browser.click("Delete");
  await browser.waitFor("!!document.querySelector('[role=dialog]')");
  await browser.click("Delete ADR");
  await browser.waitFor(text("ADR deleted. Updating repository knowledge."));
  await link("Overview");
  await browser.waitFor(
    "document.querySelector('a[aria-current=page]')?.getAttribute('aria-label') === 'Overview'"
  );
  assert.equal(await browser.evaluate(banner), false);
  await browser.click("Refresh repository knowledge");
  await browser.waitFor(
    "Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === 'Refresh repository knowledge' && !b.disabled)"
  );
  assert.equal(await browser.evaluate(banner), false);
  fixture.ready = true;
  const before = inventories().length;
  await browser.click("Refresh repository knowledge");
  await browser.waitFor(text("Architecture Overview"));
  assert.equal(await browser.evaluate(banner), false);
  assert.equal(inventories().length, before);
  assert.equal(
    await browser.evaluate(`localStorage.getItem('${prefix}usr_alice:repo_a')`),
    null
  );
});

test("Batch 4 lazy page has themed download feedback and manual recovery from a missing chunk", async () => {
  fixture.profileDelay = 700;
  fixture.profileFailure = true;
  await browser.navigate(base + "/settings");
  await browser.waitFor(
    "document.body.innerText.includes('Loading My Profile...') && document.querySelector('[role=status][aria-busy=true] [data-slot=skeleton]') !== null"
  );
  await browser.waitFor(
    "document.body.innerText.includes('This page could not be loaded')"
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[role=alert] button').textContent.trim()"
    ),
    "Reload page"
  );
  const failures = fixture.pageFiles.filter(
    (p) => p === "/src/pages/settings.tsx"
  ).length;
  await browser.evaluate("new Promise(resolve => setTimeout(resolve, 250))");
  assert.equal(
    fixture.pageFiles.filter((p) => p === "/src/pages/settings.tsx").length,
    failures
  );
  fixture.profileFailure = false;
  await browser.click("Reload page");
  await browser.waitFor(
    "document.body.innerText.includes('Not provided by GitHub')"
  );
  assert.equal(await browser.evaluate("location.pathname"), "/settings");
  assert.equal(
    await browser.evaluate(
      "document.body.innerText.includes('This page could not be loaded')"
    ),
    false
  );
});

test("Batch 4 dashboard does not eagerly download feature page modules", async () => {
  await browser.navigate(base + "/projects");
  await browser.waitFor(
    "document.body.innerText.includes('No repositories') || document.body.innerText.includes('Connect') || document.querySelector('[aria-label=\"Open profile menu\"]') !== null"
  );
  for (const page of [
    "settings",
    "install",
    "repository-decisions",
    "repository-pull-requests",
    "repository-pull-request-detail",
    "explore",
  ]) {
    assert.equal(
      fixture.pageFiles.includes("/src/pages/" + page + ".tsx"),
      false,
      page
    );
  }
});
