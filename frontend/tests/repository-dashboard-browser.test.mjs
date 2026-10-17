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
    monitoring: tracked
      ? {
          github_admin: true,
          can_enable_monitoring: status === "IDLE",
          can_retry_indexing: status === "FAILED",
          reason: "allowed",
        }
      : {
          github_admin: null,
          can_enable_monitoring: null,
          can_retry_indexing: false,
          reason: "permission_unknown",
        },
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
          vite.middlewares.use((request, response, next) => {
            if (request.url === "/__auth_test_reset") {
              response.setHeader("Content-Type", "text/html");
              return response.end("<p>Reset authentication fixture</p>");
            }
            if (request.url !== "/auth/github/login") return next();
            response.setHeader("Content-Type", "text/html");
            response.end(
              `<script>window.opener.postMessage(${JSON.stringify({
                type: "decisionguard.auth",
                token: fixture.loginToken ?? aliceToken,
                next: "/projects",
                installation_outcome: fixture.loginOutcome ?? null,
              })}, ${JSON.stringify(base)}); window.close();</script>`
            );
          });
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
              if (request.url === "/repositories/ingest") {
                let body = "";
                for await (const chunk of request) body += chunk;
                const payload = JSON.parse(body);
                current.calls.at(-1).body = payload;
                const row = current.repositories.find(
                  (r) =>
                    r.full_name === payload.repository &&
                    r.installation_id === payload.installation_id
                );
                if (
                  !row ||
                  row.monitoring?.can_enable_monitoring !== true ||
                  row.monitoring?.github_admin !== true
                )
                  return json(
                    {
                      detail:
                        "GitHub repository administrator permission is required",
                    },
                    403
                  );
                row.id = row.tracked_repository_id =
                  "repo_" + row.github_repository_id;
                if (!current.syncAcceptedOnly) {
                  row.status = "INDEXING";
                  row.monitoring.can_enable_monitoring = false;
                }
                return json(
                  {
                    status: "accepted",
                    repository_id: row.id,
                    repository: row.full_name,
                  },
                  202
                );
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
                if (current.syncDelay)
                  await new Promise((resolve) =>
                    setTimeout(resolve, current.syncDelay)
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
    source: "localStorage.setItem('theme', 'light');",
  });
  screenshots = await mkdtemp(
    path.join(tmpdir(), "decisionguard-dashboard-qa-")
  );
});
beforeEach(async () => {
  fixture = setup();
  await browser.navigate(base + "/__auth_test_reset");
  await browser.evaluate(
    `localStorage.setItem('token', ${JSON.stringify(aliceToken)})`
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
  fixture.loginToken = token;
  fixture.loginOutcome = outcome;
  await browser.navigate(base + "/__auth_test_reset");
  await browser.evaluate("localStorage.removeItem('token')");
  await browser.navigate(base + "/auth/login");
  await browser.waitFor(bodyContains("Login with Github"));
  await browser.click("Login with Github");
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
    await browser.evaluate(card("101") + ".innerText.includes('Monitored')"),
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
      card("102") + ".innerText.includes('Enable Monitoring')"
    ),
    false
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('a[href^=\"/projects/\"]').length"
    ),
    1
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

test("ready allowed card replaces the status drawer action with canonical workspace navigation", async () => {
  await visit();
  await browser.waitFor(card("101") + " !== null");
  const link =
    card("101") + ".querySelector('a[href=\"/projects/repo_101/overview\"]')";
  assert.equal(
    await browser.evaluate(link + ".innerText.trim()"),
    "Open Repository"
  );
  assert.equal(
    await browser.evaluate(card("101") + ".innerText.includes('View Status')"),
    false
  );
  assert.equal(
    await browser.evaluate(
      card("101") + ".querySelectorAll('a[href^=\"/projects/\"]').length"
    ),
    1
  );
  await browser.evaluate(link + ".click()");
  await browser.waitFor(pathIs("/projects/repo_101/overview"));
  assert.equal(
    await browser.evaluate("document.querySelector('[role=dialog]') !== null"),
    false
  );
  // This fixture exposes no stored-knowledge endpoint: navigation must not
  // grant access based solely on the card's allowed monitoring capability.
  await browser.waitFor(bodyContains("Repository not found"));
  assert.equal(
    await browser.evaluate(bodyContains("Architecture Overview")),
    false
  );
});

test("a single collaborator-accessible repository has no administrative actions", async () => {
  fixture.repositories = [repository("102", "example-org/shared")];
  await visit();
  await browser.waitFor(
    "document.querySelectorAll('[data-repository-id]').length === 1"
  );
  assert.equal(
    await browser.evaluate(bodyContains("Enable Monitoring")),
    false
  );
  assert.equal(await browser.evaluate(bodyContains("Retry Monitoring")), false);
  assert.equal(
    await browser.evaluate(
      bodyContains("Monitoring permissions could not be verified")
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
  await browser.click("Enable Monitoring");
  await browser.waitFor(card("105") + ".innerText.includes('Indexing')");
  const enabled = fixture.calls.find(
    (c) => c.url === "/repositories/alice/registered/sync"
  );
  assert.deepEqual(enabled.body, { force_full: false, sync: false });
  await browser.click("Retry Monitoring");
  await browser.waitFor(card("104") + ".innerText.includes('Indexing')");
  fixture.repositories.find((r) => r.github_repository_id === "105").status =
    "COMPLETED";
  await browser.click("Refresh repositories");
  await browser.waitFor(card("105") + ".innerText.includes('Monitored')");
});

test("accepted requests do not fabricate indexing progress and denied actions remain errors", async () => {
  fixture.syncAcceptedOnly = true;
  await visit();
  await browser.waitFor(card("105") + " !== null");
  await browser.click("Enable Monitoring");
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
    "Array.from(document.querySelectorAll('button')).some(b => b.innerText.trim() === 'Retry Monitoring' && !b.disabled)"
  );
  await browser.click("Retry Monitoring");
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
  await browser.waitFor(bodyContains("Invalid URL"));
  assert.equal(await browser.evaluate(pathIs("/install")), true);
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

test("manual status checks update only the selected repository without polling", async () => {
  fixture.repositories = [
    repository("103", "example-org/worker", "INDEXING", true, "20"),
    repository("109", "example-org/second", "INDEXING", true, "20"),
    repository("101", "alice/api", "COMPLETED", true),
  ];
  await visit();
  await browser.waitFor(card("103") + ".innerText.includes('Check Status')");
  const requests = fixture.calls.filter(
    (c) => c.url === "/repositories"
  ).length;
  fixture.repositories[0].status = "COMPLETED";
  fixture.repositories[1].status = "COMPLETED";
  // Wait past the former 10-second polling interval; no background transition is allowed.
  await new Promise((resolve) => setTimeout(resolve, 10_500));
  assert.equal(
    fixture.calls.filter((c) => c.url === "/repositories").length,
    requests
  );
  assert.equal(
    await browser.evaluate(card("103") + ".innerText.includes('Indexing')"),
    true
  );
  await browser.evaluate(card("103") + ".querySelector('button').click()");
  await browser.waitFor(card("103") + ".innerText.includes('Monitored')");
  assert.equal(
    await browser.evaluate(card("109") + ".innerText.includes('Indexing')"),
    true
  );
  assert.equal(
    fixture.calls.filter((c) => c.url === "/repositories").length,
    requests + 1
  );
  assert.equal(
    await browser.evaluate(
      card("103") +
        ".innerText.includes('Repository indexing completed successfully.')"
    ),
    false
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('a[href^=\"/projects/\"]').length"
    ),
    2
  );
  assert.equal(
    fixture.calls.some((c) => c.method === "POST"),
    false
  );
  await browser.navigate(base + "/projects");
  await browser.waitFor(card("109") + ".innerText.includes('Monitored')");
});

test("verified GitHub admin enables a previously untracked repository and sees server progress", async () => {
  const row = repository("301", "alice/profile");
  row.monitoring = {
    github_admin: true,
    can_enable_monitoring: true,
    can_retry_indexing: false,
    reason: "allowed",
  };
  fixture.repositories = [row];
  await visit();
  await browser.waitFor(bodyContains("Enable Monitoring"));
  assert.equal(
    fixture.calls.some((c) => c.method === "POST"),
    false
  );
  assert.equal(
    await browser.evaluate(bodyContains("permissions could not be verified")),
    false
  );
  await browser.click("Enable Monitoring");
  await browser.waitFor(card("301") + ".innerText.includes('Indexing')");
  const request = fixture.calls.find((c) => c.url === "/repositories/ingest");
  assert.deepEqual(request.body, {
    repository: "alice/profile",
    installation_id: "10",
    branch: "main",
    force_full: false,
    sync: false,
  });
  row.status = "COMPLETED";
  await browser.click("Refresh repositories");
  await browser.waitFor(card("301") + ".innerText.includes('Monitored')");
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('a[href^=\"/projects/\"]').length"
    ),
    1
  );
});

test("explicitly restricted administrators and non-admin collaborators have no monitoring action", async () => {
  for (const [reason, admin] of [
    ["github_admin_required", false],
    ["owned_by_another_user", true],
  ]) {
    const row = repository("302", "alice/profile");
    row.monitoring = {
      github_admin: admin,
      can_enable_monitoring: false,
      can_retry_indexing: false,
      reason,
    };
    fixture.repositories = [row];
    await visit();
    await browser.waitFor(card("302") + ".innerText.includes('Restricted')");
    assert.equal(
      await browser.evaluate(bodyContains("Enable Monitoring")),
      false
    );
    assert.equal(
      await browser.evaluate(bodyContains("Retry Monitoring")),
      false
    );
    assert.equal(
      fixture.calls.some((c) => c.method === "POST"),
      false
    );
  }
});

test("status failures preserve indexing and can be retried to a reported failure", async () => {
  fixture.repositories = [
    repository("103", "example-org/worker", "INDEXING", true, "20"),
  ];
  await visit();
  await browser.waitFor(bodyContains("Check Status"));
  fixture.failure = { status: 503, detail: "GitHub temporarily unavailable" };
  await browser.click("Check Status");
  await browser.waitFor(
    card("103") + ".querySelector('[role=alert]') !== null"
  );
  assert.equal(
    await browser.evaluate(card("103") + ".innerText.includes('Indexing')"),
    true
  );
  assert.equal(
    await browser.evaluate(bodyContains("GitHub temporarily unavailable")),
    true
  );
  assert.equal(
    await browser.evaluate(bodyContains("Unable to refresh repositories")),
    false
  );
  fixture.failure = null;
  fixture.repositories[0].status = "FAILED";
  fixture.repositories[0].monitoring = {
    github_admin: false,
    can_enable_monitoring: false,
    can_retry_indexing: false,
    reason: "github_admin_required",
  };
  await browser.click("Check Status");
  await browser.waitFor(card("103") + ".innerText.includes('View Error')");
  assert.equal(
    await browser.evaluate(card("103") + ".innerText.includes('Failed')"),
    true
  );
  assert.equal(await browser.evaluate(bodyContains("Retry Monitoring")), false);
  await browser.click("View Error");
  await browser.waitFor("document.querySelector('[role=dialog]') !== null");
  assert.equal(
    await browser.evaluate(
      bodyContains("Detailed ingestion errors are not exposed")
    ),
    true
  );
});

test("concurrent indexing cards have independent loading and remain indexing until confirmed", async () => {
  fixture.repositories = [
    repository("103", "example-org/worker", "INDEXING", true, "20"),
    repository("109", "example-org/second", "INDEXING", true, "20"),
  ];
  await visit();
  await browser.waitFor(card("109") + ".innerText.includes('Check Status')");
  fixture.delay = 700;
  const count = fixture.calls.filter((c) => c.url === "/repositories").length;
  await browser.evaluate(card("103") + ".querySelector('button').click()");
  await browser.waitFor(
    card("103") + ".innerText.includes('Checking status...')"
  );
  assert.equal(
    await browser.evaluate(card("109") + ".querySelector('button').disabled"),
    false
  );
  await browser.evaluate(card("109") + ".querySelector('button').click()");
  await browser.waitFor(
    card("109") + ".innerText.includes('Checking status...')"
  );
  await browser.waitFor(
    card("103") +
      ".innerText.includes('Check Status') && !" +
      card("103") +
      ".querySelector('button').disabled"
  );
  await browser.waitFor(
    card("109") +
      ".innerText.includes('Check Status') && !" +
      card("109") +
      ".querySelector('button').disabled"
  );
  assert.equal(
    fixture.calls.filter((c) => c.url === "/repositories").length,
    count + 2
  );
  assert.equal(
    await browser.evaluate(
      card("103") +
        ".innerText.includes('Indexing') && " +
        card("109") +
        ".innerText.includes('Indexing')"
    ),
    true
  );
});

test("access sheets are keyboard accessible and cards align their footers", async () => {
  fixture.repositories[0].description = null;
  fixture.repositories[1].description = "A long repository description ".repeat(
    20
  );
  await visit();
  await browser.waitFor(card("105") + " !== null");
  const dimensions = await browser.evaluate(
    "Array.from(document.querySelectorAll('[data-repository-id]')).slice(0,3).map(c => ({height:c.getBoundingClientRect().height, footer:c.querySelector('[data-slot=card-footer]').getBoundingClientRect().bottom}))"
  );
  assert.ok(
    dimensions.every((d) => Math.abs(d.height - dimensions[0].height) < 1)
  );
  assert.ok(
    dimensions.every((d) => Math.abs(d.footer - dimensions[0].footer) < 1)
  );
  await browser.evaluate(card("102") + ".querySelector('button').focus()");
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "Enter",
    code: "Enter",
    windowsVirtualKeyCode: 13,
    text: "\r",
  });
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "Enter",
    code: "Enter",
    windowsVirtualKeyCode: 13,
  });
  await browser.waitFor("document.querySelector('[role=dialog]') !== null");
  assert.equal(
    await browser.evaluate(
      bodyContains("Monitoring permissions could not be verified")
    ),
    true
  );
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
  await browser.waitFor("document.querySelector('[role=dialog]') === null");
  assert.equal(
    await browser.evaluate(card("102") + ".contains(document.activeElement)"),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('a[target=\"_blank\"]').length"
    ),
    5
  );
});

test("activation prevents duplicate requests without blocking other cards", async () => {
  fixture.repositories = [
    repository("105", "alice/registered", "IDLE", true),
    repository("110", "alice/other", "IDLE", true),
  ];
  await visit();
  await browser.waitFor(
    card("110") + ".innerText.includes('Enable Monitoring')"
  );
  fixture.syncDelay = 700;
  await browser.evaluate(
    card("105") +
      ".querySelector('button').click(); " +
      card("105") +
      ".querySelector('button').click()"
  );
  await browser.waitFor(
    card("105") + ".innerText.includes('Requesting indexing...')"
  );
  assert.equal(
    await browser.evaluate(card("105") + ".querySelector('button').disabled"),
    true
  );
  assert.equal(
    await browser.evaluate(card("110") + ".querySelector('button').disabled"),
    false
  );
  await browser.waitFor(card("105") + ".innerText.includes('Check Status')");
  assert.equal(
    fixture.calls.filter((c) => c.url === "/repositories/alice/registered/sync")
      .length,
    1
  );
});

test("accepted activation with failed status refresh stays recoverable without a duplicate activation", async () => {
  fixture.repositories = [repository("105", "alice/registered", "IDLE", true)];
  await visit();
  await browser.waitFor(bodyContains("Enable Monitoring"));
  fixture.failure = { status: 503, detail: "GitHub discovery unavailable" };
  await browser.click("Enable Monitoring");
  await browser.waitFor(bodyContains("Check Status to retry verification"));
  assert.equal(
    await browser.evaluate(card("105") + ".innerText.includes('Available')"),
    true
  );
  assert.equal(
    await browser.evaluate(bodyContains("GitHub discovery unavailable")),
    true
  );
  assert.equal(
    await browser.evaluate(bodyContains("Enable Monitoring")),
    false
  );
  fixture.failure = null;
  await browser.click("Check Status");
  await browser.waitFor(card("105") + ".innerText.includes('Indexing')");
  assert.equal(fixture.calls.filter((c) => c.method === "POST").length, 1);
});
