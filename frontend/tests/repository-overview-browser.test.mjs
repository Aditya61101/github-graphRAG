import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createServer } from "vite";
import { openBrowser } from "./browser-helper.mjs";

let browser, server, base, fixture, screenshots;
const token =
  "e30." +
  Buffer.from(
    JSON.stringify({ sub: "usr_alice", exp: Date.now() / 1000 + 3600 })
  ).toString("base64url") +
  ".test";
const unavailable = { status: "unavailable", reason: "engine_not_implemented" };
function setup() {
  return {
    delay: 0,
    failure: null,
    calls: [],
    overview: {
      repository: {
        id: "repo_101",
        owner: "alice",
        name: "api",
        full_name: "alice/api",
        repository_url: "https://github.com/alice/api",
        tracked_branch: "develop",
        indexed_commit_sha: "abcdef123456789",
        status: "COMPLETED",
      },
      permissions: { can_view_settings: true },
      decision_count_basis: "distinct_persisted_nodes",
      kpis: {
        architecture_decisions: {
          value: 12,
          availability: { status: "available", reason: null },
        },
        decisions_at_risk: { value: null, availability: unavailable },
        prs_analyzed: { value: null, availability: unavailable },
        architectural_warnings: { value: null, availability: unavailable },
      },
      review_activity: { availability: unavailable, window_days: 7 },
      decisions_needing_attention: { availability: unavailable },
      recent_pr_reviews: { availability: unavailable },
    },
  };
}
before(async () => {
  fixture = setup();
  server = await createServer({
    server: { host: "127.0.0.1", port: 0, hmr: false },
    define: {
      "import.meta.env.VITE_BACKEND_URL": JSON.stringify("/__overview_api"),
    },
    plugins: [
      {
        name: "overview-test-api",
        configureServer(vite) {
          vite.middlewares.use("/__overview_api", async (request, response) => {
            const current = fixture;
            current.calls.push({
              method: request.method,
              url: request.url,
              bearer: request.headers.authorization,
            });
            response.setHeader("Content-Type", "application/json");
            const json = (data, status = 200) => {
              response.statusCode = status;
              response.end(JSON.stringify(data));
            };
            if (request.url === "/auth/me")
              return json({
                onboarding_completed: true,
                onboarding_completed_at: "2026-10-11T00:00:00",
                next: "/projects",
                user: {
                  id: "usr_alice",
                  username: "alice",
                  avatar_url: null,
                  email: null,
                },
                installations: [],
                installation_discovery_status: "not_requested",
                reconnect_required: false,
                installation_onboarding_required: false,
              });
            if (request.url === "/repositories/repo_101/overview") {
              if (current.delay)
                await new Promise((resolve) =>
                  setTimeout(resolve, current.delay)
                );
              if (current.failure)
                return json(
                  { detail: "Access or backend unavailable" },
                  current.failure
                );
              return json(current.overview);
            }
            if (request.url.startsWith("/repositories/repo_101/pull-requests"))
              return json({ items: [], total: 0, limit: 20, offset: 0 });
            if (request.url === "/repositories")
              return json([
                {
                  ...current.overview.repository,
                  github_repository_id: "101",
                  tracked_repository_id: "repo_101",
                  installation_id: "10",
                  default_branch: "main",
                  is_private: false,
                  description: null,
                  updated_at: null,
                  monitoring: {
                    github_admin: true,
                    can_enable_monitoring: false,
                    can_retry_indexing: false,
                    reason: "allowed",
                  },
                },
              ]);
            return json({ detail: "Repository not found" }, 404);
          });
        },
      },
    ],
  });
  await server.listen();
  base = "http://127.0.0.1:" + server.httpServer.address().port;
  browser = await openBrowser();
  await browser.call("Page.addScriptToEvaluateOnNewDocument", {
    source: `localStorage.setItem('token', ${JSON.stringify(token)}); localStorage.setItem('theme', 'dark');`,
  });
  screenshots = await mkdtemp(
    path.join(tmpdir(), "decisionguard-overview-qa-")
  );
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
  await browser?.close();
  await server?.close();
});
const contains = (text) =>
  `document.body.innerText.includes(${JSON.stringify(text)})`;
const at = (route) => `location.pathname === ${JSON.stringify(route)}`;
async function visit(route = "/projects/repo_101/overview") {
  await browser.navigate(base + route);
}
async function ready() {
  await browser.waitFor(
    at("/projects/repo_101/overview") +
      " && " +
      contains("Architecture Overview")
  );
}
async function clickLink(label) {
  await browser.waitFor(
    `Array.from(document.querySelectorAll('a')).some(a => a.innerText.trim() === ${JSON.stringify(label)} || a.getAttribute('aria-label') === ${JSON.stringify(label)})`
  );
  await browser.evaluate(
    `Array.from(document.querySelectorAll('a')).find(a => a.innerText.trim() === ${JSON.stringify(label)} || a.getAttribute('aria-label') === ${JSON.stringify(label)}).click()`
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

test("canonical index redirect, actual KPIs, section order and repository metadata", async () => {
  await visit("/projects/repo_101");
  await ready();
  assert.equal(await browser.evaluate(at("/projects/repo_101/overview")), true);
  assert.deepEqual(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('main h1,main h2')).map(e=>e.textContent)"
    ),
    [
      "Architecture Overview",
      "Architectural Decisions",
      "Decisions at Risk",
      "PRs Analyzed",
      "Architectural Warnings",
      "Decisions needing attention",
      "Recent architectural PR reviews",
    ]
  );
  assert.deepEqual(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('[data-availability]')).map(e=>e.textContent)"
    ),
    [
      "12",
      "Not evaluated yet",
      "Analysis not available yet",
      "Analysis not available yet",
    ]
  );
  assert.equal(
    await browser.evaluate(contains("Architectural review activity")),
    false
  );
  assert.equal(await browser.evaluate(contains("abcdef1")), true);
  assert.equal(
    await browser.evaluate(contains("Tracked branch: develop")),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('a[target=\"_blank\"]').href"
    ),
    "https://github.com/alice/api"
  );
  assert.equal(
    await browser.evaluate("document.querySelectorAll('header').length"),
    1
  );
  assert.equal(
    await browser.evaluate(
      "getComputedStyle(document.querySelector('[aria-label=\"Architecture metrics\"]')).gridTemplateColumns.split(' ').length"
    ),
    2
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[data-slot=sidebar]').dataset.state"
    ),
    "expanded"
  );
  assert.ok(
    fixture.calls.some(
      (c) =>
        c.url === "/repositories/repo_101/overview" &&
        c.bearer === "Bearer " + token
    )
  );
  assert.equal(
    fixture.calls.some((c) => c.method !== "GET"),
    false
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('canvas, [role=img]').length"
    ),
    0
  );
  await capture("overview-desktop-dark.png");
});

test("sidebar collapse, keyboard tooltip, active route and nested placeholders preserve shell", async () => {
  await visit();
  await ready();
  await browser.click("Toggle Sidebar");
  await browser.waitFor(
    "document.querySelector('[data-slot=sidebar]').dataset.state === 'collapsed'"
  );
  await browser.evaluate(
    "document.querySelector('a[aria-label=\"Knowledge Explorer\"]').focus()"
  );
  await browser.waitFor(
    "document.querySelector('[data-slot=tooltip-content]')?.textContent.includes('Knowledge Explorer')"
  );
  await clickLink("Knowledge Explorer");
  await browser.waitFor(at("/projects/repo_101/explore"));
  await browser.waitFor(contains("Architecture Chat"));
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[data-slot=sidebar]').dataset.state"
    ),
    "collapsed"
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('a[aria-current=page]').getAttribute('aria-label')"
    ),
    "Knowledge Explorer"
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('a[aria-current=page]').hasAttribute('data-active')"
    ),
    true
  );
  await clickLink("Overview");
  await ready();
  await clickLink("View all decisions");
  await browser.waitFor(
    at("/projects/repo_101/decisions") +
      " && " +
      contains("Architectural Knowledge") +
      " && document.querySelector('main h1')?.textContent === 'Decisions'"
  );
  await clickLink("Overview");
  await ready();
  await clickLink("View all PRs");
  await browser.waitFor(
    at("/projects/repo_101/pull-requests") +
      " && " +
      contains("No observed pull requests") +
      " && document.querySelector('main h1')?.textContent === 'Pull Requests'"
  );
  assert.equal(
    await browser.evaluate(
      "!!document.querySelector('a[aria-label=Settings]')"
    ),
    false
  );
  assert.equal(
    fixture.calls.filter((c) => c.url.endsWith("/overview")).length,
    1
  );
});

test("zero is rendered as a count and graph failure is rendered as unavailable", async () => {
  fixture.overview.kpis.architecture_decisions.value = 0;
  await visit();
  await ready();
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[data-availability]').textContent"
    ),
    "0"
  );
  fixture.overview.kpis.architecture_decisions = {
    value: null,
    availability: { status: "error", reason: "graph_query_failed" },
  };
  await visit();
  await ready();
  assert.equal(
    await browser.evaluate(
      "document.querySelector('[data-availability]').textContent"
    ),
    "Count unavailable"
  );
  assert.equal(
    await browser.evaluate(contains("The graph query failed")),
    true
  );
});

test("loading, authorization, unknown repository and temporary failure with retry", async () => {
  fixture.delay = 700;
  await visit();
  await browser.waitFor(contains("Loading repository workspace"));
  await ready();
  for (const [code, title] of [
    [403, "Repository access denied"],
    [404, "Repository not found"],
    [503, "Unable to load repository"],
  ]) {
    fixture.delay = 0;
    fixture.failure = code;
    await visit();
    await browser.waitFor(contains(title));
    assert.equal(
      await browser.evaluate(contains("Architecture Overview")),
      false
    );
    assert.equal(await browser.evaluate(contains("alice/api")), false);
  }
  fixture.failure = null;
  await browser.click("Try again");
  await ready();
  await visit("/projects/101/overview");
  await browser.waitFor(contains("Invalid repository ID"));
  assert.equal(
    fixture.calls.some((c) => c.url === "/repositories/101/overview"),
    false
  );
});

test("settings is hidden and its legacy URL redirects to Overview without requiring admin", async () => {
  fixture.overview.permissions.can_view_settings = false;
  await visit();
  await ready();
  assert.equal(
    await browser.evaluate(
      "document.querySelector('a[aria-label=Settings]') !== null"
    ),
    false
  );
  await visit("/projects/repo_101/settings");
  await ready();
  assert.equal(await browser.evaluate(contains("Coming later")), false);
});

test("failed and incomplete indexing distinguish current attempt from last successful revision", async () => {
  fixture.overview.repository.status = "FAILED";
  await visit();
  await ready();
  assert.equal(
    await browser.evaluate(contains("latest indexing attempt failed")),
    true
  );
  assert.equal(
    await browser.evaluate(
      contains("Last successfully indexed commit: abcdef1")
    ),
    true
  );
  fixture.overview.repository.status = "INDEXING";
  fixture.overview.repository.indexed_commit_sha = null;
  await visit();
  await ready();
  assert.equal(
    await browser.evaluate(contains("Indexing is incomplete")),
    true
  );
  assert.equal(await browser.evaluate(contains("Not indexed yet")), true);
});

test("Open Repository and All Repositories use canonical routes", async () => {
  await visit("/projects");
  await browser.waitFor(contains("Open Repository"));
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('a')).find(a=>a.innerText.trim()==='Open Repository').getAttribute('href')"
    ),
    "/projects/repo_101/overview"
  );
  await clickLink("Open Repository");
  await ready();
  await clickLink("All Repositories");
  await browser.waitFor(at("/projects"));
});

test("mobile drawer, navigation and dark theme do not overflow", async () => {
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 360,
    height: 800,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await visit();
  await ready();
  assert.equal(
    await browser.evaluate(
      "document.documentElement.classList.contains('dark')"
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.documentElement.scrollWidth <= innerWidth"
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      "getComputedStyle(document.querySelector('[aria-label=\"Architecture metrics\"]')).gridTemplateColumns.split(' ').length"
    ),
    1
  );
  await capture("overview-mobile-dark.png");
  await browser.click("Toggle Sidebar");
  await browser.waitFor("document.querySelector('[role=dialog]') !== null");
  await capture("overview-mobile-drawer.png");
  await clickLink("Decisions");
  await browser.waitFor(at("/projects/repo_101/decisions"));
  await browser.waitFor("document.querySelector('[role=dialog]') === null");
  await browser.waitFor(contains("Architectural Knowledge"));
  assert.equal(
    await browser.evaluate(contains("Architectural Knowledge")),
    true
  );
});

test("profile menu and logout preserve existing behavior and clear workspace content", async () => {
  await visit();
  await ready();
  await browser.evaluate(
    "document.querySelector('[aria-label=\"Open profile menu\"]').click()"
  );
  await browser.waitFor(contains("My Profile"));
  await browser.evaluate(
    "Array.from(document.querySelectorAll('[role=menuitem]')).find(e=>e.innerText.trim()==='My Profile').click()"
  );
  await browser.waitFor(at("/settings"));
  await visit();
  await ready();
  await browser.evaluate(
    "document.querySelector('[aria-label=\"Open profile menu\"]').click()"
  );
  await browser.waitFor(contains("Log out"));
  await browser.evaluate(
    "Array.from(document.querySelectorAll('[role=menuitem]')).find(e=>e.innerText.trim()==='Log out').click()"
  );
  await browser.waitFor(at("/auth/login"));
  assert.equal(await browser.evaluate("localStorage.getItem('token')"), null);
  assert.equal(
    await browser.evaluate(contains("Architecture Overview")),
    false
  );
  console.log("Overview visual checks: " + screenshots);
});
