import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createServer } from "vite";
import { openBrowser } from "./browser-helper.mjs";

let browser, server, base, fixture, screenshots;
const tokenFor = (id) =>
  "e30." +
  Buffer.from(
    JSON.stringify({ sub: id, exp: Date.now() / 1000 + 3600 })
  ).toString("base64url") +
  ".test";
const aliceToken = tokenFor("usr_alice");
const bobToken = tokenFor("usr_bob");
const completion = {
  onboarding_completed: true,
  onboarding_completed_at: "2026-10-11T00:00:00",
  next: "/projects",
};
function repository(
  id,
  full_name,
  status = "NOT_INDEXED",
  tracked = false,
  installation_id = "10"
) {
  const [owner, name] = full_name.split("/");
  return {
    id: tracked ? "repo_" + id : null,
    tracked_repository_id: tracked ? "repo_" + id : null,
    github_repository_id: id,
    installation_id,
    owner,
    name,
    full_name,
    repository_url: "https://github.com/" + full_name,
    default_branch: "main",
    tracked_branch: tracked ? "develop" : "main",
    is_private: false,
    description: "Public source code for " + name,
    status,
    indexed_commit_sha: status === "COMPLETED" ? "abc123" : null,
    updated_at: null,
  };
}
function setup() {
  return {
    repositories: [
      repository("101", "alice/api", "COMPLETED", true),
      repository("102", "example-org/shared", "NOT_INDEXED", false, "20"),
      repository("103", "example-org/worker", "INDEXING", true, "20"),
      repository("104", "alice/broken", "FAILED", true),
      repository("105", "alice/registered", "IDLE", true),
    ],
    bobRepositories: [repository("201", "bob/only-repository")],
    delay: 0,
    failure: null,
    syncFailure: false,
    syncAcceptedOnly: false,
    reconnect: false,
    calls: [],
  };
}
before(async () => {
  fixture = setup();
  server = await createServer({
    server: { host: "127.0.0.1", port: 0, hmr: false },
    define: {
      "import.meta.env.VITE_BACKEND_URL": JSON.stringify("/__dashboard_api"),
    },
    plugins: [
      {
        name: "dashboard-test-api",
        configureServer(vite) {
          vite.middlewares.use(
            "/__dashboard_api",
            async (request, response) => {
              const current = fixture;
              const bearer = request.headers.authorization ?? "";
              const bob = bearer.includes(bobToken);
              current.calls.push({
                method: request.method,
                url: request.url,
                bearer,
              });
              response.setHeader("Content-Type", "application/json");
              const json = (data, status = 200) => {
                response.statusCode = status;
                response.end(JSON.stringify(data));
              };
              if (request.url === "/auth/me")
                return json({
                  ...completion,
                  user: {
                    id: bob ? "usr_bob" : "usr_alice",
                    username: bob ? "bob" : "alice",
                    avatar_url: null,
                    email: null,
                    onboarding_completed_at: completion.onboarding_completed_at,
                  },
                  installations: [],
                  installation_discovery_status: "not_requested",
                  reconnect_required: current.reconnect,
                  installation_onboarding_required: false,
                });
              if (request.url === "/repositories") {
                const rows = bob
                  ? current.bobRepositories
                  : current.repositories;
                if (current.delay)
                  await new Promise((resolve) =>
                    setTimeout(resolve, current.delay)
                  );
                if (current.failure)
                  return json(
                    { detail: current.failure.detail },
                    current.failure.status
                  );
                return json(rows);
              }
              if (request.url?.endsWith("/sync")) {
                if (current.syncFailure)
                  return json({ detail: "Repository permission denied" }, 403);
                let body = "";
                for await (const chunk of request) body += chunk;
                current.calls.at(-1).body = JSON.parse(body);
                const row = current.repositories.find(
                  (r) =>
                    request.url === "/repositories/" + r.full_name + "/sync"
                );
                if (row && !current.syncAcceptedOnly) row.status = "INDEXING";
                return json(
                  {
                    status: "accepted",
                    repository_id: row?.id,
                    repository: row?.full_name,
                  },
                  202
                );
              }
              if (request.url === "/auth/onboarding")
                return json({
                  ...completion,
                  eligible: true,
                  screen: "completed",
                  installation_discovery_status: "complete",
                  installation_outcome: null,
                  installations: [],
                });
              if (request.url === "/auth/github/install")
                return json({
                  installation_url:
                    base + "/__github_install?state=server-bound-state",
                });
              return json({ detail: "Unexpected test request" }, 404);
            }
          );
        },
      },
    ],
  });
  await server.listen();
  base = "http://127.0.0.1:" + server.httpServer.address().port;
  browser = await openBrowser();
  await browser.call("Page.addScriptToEvaluateOnNewDocument", {
    source:
      "localStorage.setItem('token', " +
      JSON.stringify(aliceToken) +
      "); localStorage.setItem('theme', 'light');",
  });
  screenshots = await mkdtemp(
    path.join(tmpdir(), "decisionguard-dashboard-qa-")
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
const bodyContains = (text) =>
  "document.body.innerText.includes(" + JSON.stringify(text) + ")";
const pathIs = (value) => "location.pathname === " + JSON.stringify(value);
const card = (id) =>
  "document.querySelector('[data-repository-id=\"" + id + "\"]')";
async function visit() {
  await browser.navigate(base + "/projects");
  await browser.waitFor(bodyContains("Your repositories"));
}
async function login(token = aliceToken, outcome = null) {
  await browser.navigate(
    base +
      "/auth/success?" +
      new URLSearchParams({
        token,
        ...(outcome ? { installation_outcome: outcome } : {}),
      })
  );
}
async function openProfile() {
  await browser.evaluate(
    "document.querySelector('[aria-label=\"Open profile menu\"]').click()"
  );
  await browser.waitFor(bodyContains("My profile"));
}

test("multiple installations render unique real repositories and no mock project workspace", async () => {
  fixture.repositories.push({
    ...fixture.repositories[1],
    installation_id: "30",
  });
  await visit();
  await browser.waitFor(
    "document.querySelectorAll('[data-repository-id]').length === 5"
  );
  assert.equal(await browser.evaluate(bodyContains("example-org")), true);
  assert.equal(
    await browser.evaluate(card("101") + ".innerText.includes('Ready')"),
    true
  );
  assert.equal(
    await browser.evaluate(card("103") + ".innerText.includes('Indexing')"),
    true
  );
  assert.equal(
    await browser.evaluate(card("104") + ".innerText.includes('Failed')"),
    true
  );
  assert.equal(
    await browser.evaluate(
      card("102") + ".innerText.includes('Enable monitoring')"
    ),
    false
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('a[href^=\"/projects/\"]').length"
    ),
    0
  );
  assert.equal(
    fixture.calls.some((c) => c.method === "POST"),
    false
  );
  const image = await browser.call("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
  });
  await writeFile(
    path.join(screenshots, "repositories-desktop-light.png"),
    Buffer.from(image.data, "base64")
  );
});

test("a single collaborator-accessible repository has no administrative actions", async () => {
  fixture.repositories = [repository("102", "example-org/shared")];
  await visit();
  await browser.waitFor(
    "document.querySelectorAll('[data-repository-id]').length === 1"
  );
  assert.equal(
    await browser.evaluate(bodyContains("Enable monitoring")),
    false
  );
  assert.equal(await browser.evaluate(bodyContains("Retry indexing")), false);
  assert.equal(
    await browser.evaluate(
      bodyContains("Monitoring permissions are not provided")
    ),
    true
  );
  assert.equal(
    fixture.calls.some((c) => c.method === "POST"),
    false
  );
});

test("repository search matches owner and name and combines with status filters", async () => {
  await visit();
  await browser.waitFor(
    "document.querySelectorAll('[data-repository-id]').length === 5"
  );
  await browser.evaluate(
    "var input = document.getElementById('repository-search'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'EXAMPLE-ORG'); input.dispatchEvent(new Event('input', { bubbles: true }));"
  );
  await browser.waitFor(
    "document.querySelectorAll('[data-repository-id]').length === 2"
  );
  await browser.click("Available");
  await browser.waitFor(
    "document.querySelectorAll('[data-repository-id]').length === 1"
  );
  assert.equal(await browser.evaluate(card("102") + " !== null"), true);
  await browser.click("Monitored");
  await browser.waitFor(bodyContains("No matching repositories"));
  await browser.click("Clear filters");
  await browser.waitFor(
    "document.querySelectorAll('[data-repository-id]').length === 5"
  );
});

test("returning users remain on projects during loading, successful empty discovery, and transient failure", async () => {
  fixture.delay = 800;
  fixture.repositories = [];
  await visit();
  await browser.waitFor(bodyContains("Discovering repositories..."));
  assert.equal(
    await browser.evaluate(bodyContains("No accessible repositories")),
    false
  );
  await browser.waitFor(bodyContains("No accessible repositories"));
  assert.equal(await browser.evaluate(pathIs("/projects")), true);
  fixture.failure = { status: 503, detail: "GitHub temporarily unavailable" };
  await browser.click("Refresh repositories");
  await browser.waitFor(bodyContains("Unable to refresh repositories"));
  assert.equal(
    await browser.evaluate(bodyContains("No accessible repositories")),
    false
  );
  fixture.failure = null;
  fixture.delay = 0;
  fixture.repositories = [repository("106", "alice/recovered")];
  await browser.click("Try again");
  await browser.waitFor(card("106") + " !== null");
});

test("failed refresh hides stale authorized cards and provides recovery for unavailable access", async () => {
  await visit();
  await browser.waitFor(card("101") + " !== null");
  fixture.failure = { status: 403, detail: "Installation is suspended" };
  await browser.click("Refresh repositories");
  await browser.waitFor(bodyContains("Unable to refresh repositories"));
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('[data-repository-id]').length"
    ),
    0
  );
  assert.equal(await browser.evaluate(pathIs("/projects")), true);
  assert.equal(
    await browser.evaluate(bodyContains("Check your App installation")),
    true
  );
});

test("GitHub reconnect responses preserve the app session and completed onboarding", async () => {
  fixture.failure = { status: 401, detail: "GitHub reconnect required" };
  await visit();
  await browser.waitFor(bodyContains("Reconnect GitHub"));
  assert.equal(await browser.evaluate(pathIs("/projects")), true);
  assert.equal(
    await browser.evaluate("localStorage.getItem('token')"),
    aliceToken
  );
  assert.equal(
    fixture.calls.some((c) => c.url === "/auth/onboarding/complete"),
    false
  );
});

test("monitoring and retry actions send explicit authorized sync requests and refresh status", async () => {
  await visit();
  await browser.waitFor(card("105") + " !== null");
  await browser.click("Enable monitoring");
  await browser.waitFor(card("105") + ".innerText.includes('Indexing')");
  const enabled = fixture.calls.find(
    (c) => c.url === "/repositories/alice/registered/sync"
  );
  assert.deepEqual(enabled.body, { force_full: false, sync: false });
  await browser.click("Retry indexing");
  await browser.waitFor(card("104") + ".innerText.includes('Indexing')");
  fixture.repositories.find((r) => r.github_repository_id === "105").status =
    "COMPLETED";
  await browser.click("Refresh repositories");
  await browser.waitFor(card("105") + ".innerText.includes('Ready')");
});

test("accepted requests do not fabricate indexing progress and denied actions remain errors", async () => {
  fixture.syncAcceptedOnly = true;
  await visit();
  await browser.waitFor(card("105") + " !== null");
  await browser.click("Enable monitoring");
  await browser.waitFor(bodyContains("Indexing request accepted"));
  assert.equal(
    await browser.evaluate(card("105") + ".innerText.includes('Available')"),
    true
  );
  assert.equal(
    await browser.evaluate(
      card("105") + ".innerText.includes('Indexing is in progress')"
    ),
    false
  );
  fixture.syncFailure = true;
  await browser.click("Refresh repositories");
  await browser.waitFor(
    "Array.from(document.querySelectorAll('button')).some(b => b.innerText.trim() === 'Retry indexing' && !b.disabled)"
  );
  await browser.click("Retry indexing");
  await browser.waitFor(
    card("104") + ".querySelector('[role=alert]') !== null"
  );
  assert.equal(
    await browser.evaluate(card("104") + ".innerText.includes('Failed')"),
    true
  );
});

test("connect repositories reuses installation state and refreshes discovery after success", async () => {
  await visit();
  await browser.waitFor(card("101") + " !== null");
  await browser.evaluate(
    "Array.from(document.querySelectorAll('a')).find(a => a.innerText.trim() === 'Connect repositories').click()"
  );
  await browser.waitFor(bodyContains("Manage access on GitHub"));
  await browser.click("Manage access on GitHub");
  await browser.waitFor(pathIs("/__github_install"));
  assert.equal(
    await browser.evaluate("new URLSearchParams(location.search).get('state')"),
    "server-bound-state"
  );
  fixture.repositories.push(
    repository("107", "another-org/new-repository", "NOT_INDEXED", false, "30")
  );
  const before = fixture.calls.filter((c) => c.url === "/repositories").length;
  await login(aliceToken, "success");
  await browser.waitFor(pathIs("/projects"));
  await browser.waitFor(card("107") + " !== null");
  assert.ok(
    fixture.calls.filter((c) => c.url === "/repositories").length > before
  );
  assert.equal(
    fixture.calls.some(
      (c) => c.url?.endsWith("/sync") || c.url === "/repositories/ingest"
    ),
    false
  );
});

test("profile menu navigates to the existing profile page and logout clears repository cache", async () => {
  await visit();
  await browser.waitFor(card("101") + " !== null");
  await openProfile();
  await browser.evaluate(
    "Array.from(document.querySelectorAll('[role=menuitem]')).find(e => e.innerText.trim() === 'My profile').click()"
  );
  await browser.waitFor(pathIs("/settings"));
  await browser.waitFor(
    "document.querySelector('a[href=\"/projects\"]') !== null"
  );
  // Return with client-side navigation so this check exercises the same query cache.
  await browser.evaluate(
    "document.querySelector('a[href=\"/projects\"]').click()"
  );
  await browser.waitFor(pathIs("/projects"));
  await browser.waitFor(
    "document.querySelector('[aria-label=\"Open profile menu\"]') !== null"
  );
  await openProfile();
  await browser.evaluate(
    "Array.from(document.querySelectorAll('[role=menuitem]')).find(e => e.innerText.trim() === 'Log out').click()"
  );
  await browser.waitFor(pathIs("/auth/login"));
  assert.equal(await browser.evaluate("localStorage.getItem('token')"), null);
  assert.equal(await browser.evaluate(bodyContains("example-org")), false);
  assert.equal(
    fixture.calls.some((c) => c.method === "DELETE"),
    false
  );
  await login(bobToken);
  await browser.waitFor(card("201") + " !== null");
  assert.equal(await browser.evaluate(card("101") + " !== null"), false);
  assert.equal(await browser.evaluate(bodyContains("example-org")), false);
  assert.ok(
    fixture.calls.some(
      (c) => c.url === "/repositories" && c.bearer === "Bearer " + bobToken
    )
  );
});

test("logout during discovery cancels old account results before another account signs in", async () => {
  fixture.delay = 1200;
  await visit();
  await browser.waitFor(bodyContains("Discovering repositories..."));
  await openProfile();
  await browser.evaluate(
    "Array.from(document.querySelectorAll('[role=menuitem]')).find(e => e.innerText.trim() === 'Log out').click()"
  );
  await browser.waitFor(pathIs("/auth/login"));
  await login(bobToken);
  await browser.waitFor(card("201") + " !== null");
  assert.equal(await browser.evaluate(card("101") + " !== null"), false);
});

test("repository cards and profile remain usable on mobile in dark mode", async () => {
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 360,
    height: 800,
    deviceScaleFactor: 1,
    mobile: false,
  });
  fixture.repositories.push(
    repository(
      "108",
      "long-organization-name/a-very-long-repository-name-with-many-parts"
    )
  );
  await visit();
  await browser.waitFor(card("108") + " !== null");
  await browser.evaluate("document.documentElement.classList.add('dark')");
  assert.equal(
    await browser.evaluate(
      "document.documentElement.scrollWidth <= window.innerWidth"
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('[data-slot=sidebar]').length"
    ),
    0
  );
  await openProfile();
  assert.equal(await browser.evaluate(bodyContains("Log out")), true);
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
  const image = await browser.call("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
  });
  await writeFile(
    path.join(screenshots, "repositories-mobile-dark.png"),
    Buffer.from(image.data, "base64")
  );
  console.log("Dashboard visual checks: " + screenshots);
});

test("backend indexing status is polled until the repository becomes ready", async () => {
  fixture.repositories = [
    repository("103", "example-org/worker", "INDEXING", true, "20"),
  ];
  await visit();
  await browser.waitFor(card("103") + ".innerText.includes('Indexing')");
  const requests = fixture.calls.filter(
    (c) => c.url === "/repositories"
  ).length;
  fixture.repositories[0].status = "COMPLETED";
  await browser.waitFor(card("103") + ".innerText.includes('Ready')");
  assert.ok(
    fixture.calls.filter((c) => c.url === "/repositories").length > requests
  );
  assert.equal(
    fixture.calls.some((c) => c.method === "POST"),
    false
  );
});
