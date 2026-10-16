import assert from "node:assert/strict";
import { before, beforeEach, after, test } from "node:test";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createServer } from "vite";
import { openBrowser } from "./browser-helper.mjs";

const actual = JSON.parse(
  await readFile(new URL("./fixtures/stage-4-5-case-a.json", import.meta.url))
);
const root = "/projects/repo_1120610241";
let server, browser, base, state;
const token =
  "e30." +
  Buffer.from(
    JSON.stringify({ sub: "usr_test", exp: Date.now() / 1000 + 3600 })
  ).toString("base64url") +
  ".test";
before(async () => {
  server = await createServer({
    server: { host: "127.0.0.1", port: 0, hmr: false },
    define: {
      "import.meta.env.VITE_BACKEND_URL": JSON.stringify("/__reviews_test"),
    },
    plugins: [
      {
        name: "offline-review-api",
        configureServer(vite) {
          vite.middlewares.use("/__reviews_test", async (request, response) => {
            const current = state;
            current.calls.push({ method: request.method, url: request.url });
            response.setHeader("Content-Type", "application/json");
            const json = (data, status = 200) => {
              response.statusCode = status;
              response.end(JSON.stringify(data));
            };
            if (request.method !== "GET")
              return json({ detail: "All writes forbidden" }, 405);
            const url = new URL(request.url, "http://fixture");
            if (url.pathname === "/auth/me")
              return json({
                onboarding_completed: true,
                onboarding_completed_at: "2026-10-16T00:00:00",
                next: "/projects",
                user: {
                  id: "usr_test",
                  username: "test",
                  avatar_url: null,
                  email: null,
                },
                installations: [],
                installation_discovery_status: "not_requested",
                reconnect_required: false,
                installation_onboarding_required: false,
              });
            if (url.pathname === "/repositories/repo_1120610241/overview")
              return json(current.overview);
            if (url.pathname === "/repositories/repo_1120610241/knowledge")
              return json({ items: [], total: 0, offset: 0, limit: 20 });
            if (url.pathname.startsWith("/repositories/repo_other"))
              return json({ detail: "Denied" }, 403);
            if (current.delay)
              await new Promise((resolve) =>
                setTimeout(resolve, current.delay)
              );
            if (current.failure)
              return json({ detail: "Review unavailable" }, current.failure);
            if (url.pathname.endsWith("/pull-requests")) {
              if (current.empty)
                return json({ items: [], total: 0, limit: 20, offset: 0 });
              const offset = Number(url.searchParams.get("offset"));
              return json({
                ...actual.page,
                items: offset
                  ? [
                      {
                        ...actual.detail.pull_request,
                        pr_number: 1,
                        title: "Previous PR",
                      },
                    ]
                  : actual.page.items,
                total: 21,
                limit: 20,
                offset,
              });
            }
            if (url.pathname.endsWith("/pull-requests/2")) {
              const detail = structuredClone(current.detail);
              if (current.history) {
                detail.history_total = 21;
                if (url.searchParams.get("offset") === "20")
                  detail.history = [
                    {
                      ...detail.history[0],
                      job_id: "historical-run",
                      head_sha: "a".repeat(40),
                      revision_key: "historical-revision",
                    },
                  ];
              }
              return json(detail);
            }
            if (url.pathname.endsWith("/evaluations/historical-run/result"))
              return json({
                ...actual.detail.result,
                revision: {
                  ...actual.detail.result.revision,
                  head_sha: "a".repeat(40),
                  revision_key: "historical-revision",
                },
              });
            if (
              url.pathname.endsWith(
                `/evaluations/${actual.detail.history[0].job_id}/result`
              )
            )
              return json(actual.detail.result);
            return json({ detail: "Not found" }, 404);
          });
        },
      },
    ],
  });
  await server.listen();
  base = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await openBrowser();
  await browser.call("Page.addScriptToEvaluateOnNewDocument", {
    source: `localStorage.setItem('token', ${JSON.stringify(token)}); localStorage.setItem('theme', 'dark');`,
  });
});
beforeEach(async () => {
  state = {
    overview: structuredClone(actual.overview),
    detail: structuredClone(actual.detail),
    calls: [],
    delay: 0,
    failure: null,
    empty: false,
    history: false,
  };
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
async function visit(path) {
  await browser.navigate(base + path);
}
async function link(text) {
  await browser.evaluate(
    `Array.from(document.querySelectorAll('a')).find(a => a.textContent.includes(${JSON.stringify(text)})).click()`
  );
}
async function screenshot(name) {
  const directory = new URL("../../backend/data/stage_4_5/", import.meta.url);
  await mkdir(directory, { recursive: true });
  const image = await browser.call("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
  });
  await writeFile(new URL(name, directory), Buffer.from(image.data, "base64"));
}

test("real Case A Overview, PR list, detail, Check link, original evidence and mobile layout", async () => {
  await visit(root + "/overview");
  await browser.waitFor(contains("Architecture Overview"));
  const values = await browser.evaluate(
    "Array.from(document.querySelectorAll('[data-availability]')).map(e => e.textContent)"
  );
  assert.deepEqual(
    values,
    Object.values(actual.overview.kpis).map((metric) => String(metric.value))
  );
  assert.equal(
    await browser.evaluate(contains("1 confirmed · 1 potential")),
    true
  );
  assert.equal(
    await browser.evaluate(
      contains(
        "3 supported governing-rule findings · 4 unresolved or rejected records"
      )
    ),
    true
  );
  await screenshot("overview-desktop.png");
  await link("View all PRs");
  await browser.waitFor(contains(actual.detail.pull_request.title));
  assert.equal(
    await browser.evaluate(
      contains(
        "3 supported governing-rule findings · 1 requiring review · 3 rejected records"
      )
    ),
    true
  );
  await link("#2 ");
  await browser.waitFor(contains("Confirmed architectural conflicts (3)"));
  assert.equal(
    await browser.evaluate(contains("Additional review required (1)")),
    true
  );
  assert.equal(await browser.evaluate(contains("6 uncertain")), true);
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('a')).some(a => a.href === 'https://github.com/Aditya61101/claimIQ/runs/116733676654')"
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('a[aria-label=\"Pull Requests\"]').getAttribute('aria-current')"
    ),
    "page"
  );
  await browser.evaluate(
    "Array.from(document.querySelectorAll('summary')).find(e=>e.textContent === 'Original source evidence and governing ADR').click()"
  );
  await browser.waitFor(contains("No verified public source link"));
  await browser.evaluate(
    "Array.from(document.querySelectorAll('summary')).find(e=>e.textContent === 'Base, merge-base and head comparison').click()"
  );
  await browser.waitFor(contains("Merge base:"));
  await screenshot("details-desktop.png");
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  assert.equal(
    await browser.evaluate(
      "document.documentElement.scrollWidth <= window.innerWidth"
    ),
    true
  );
  await screenshot("details-mobile.png");
  assert.ok(state.calls.every((call) => call.method === "GET"));
  assert.equal(
    state.calls.some((call) => /continue|publish/.test(call.url)),
    false
  );
});
test("PR pagination loads a separate offset and repository isolation blocks foreign routes", async () => {
  await visit(root + "/pull-requests");
  await browser.waitFor(contains(actual.detail.pull_request.title));
  await browser.click("Next");
  await browser.waitFor(contains("Previous PR"));
  assert.ok(state.calls.some((call) => call.url.includes("offset=20")));
  await visit("/projects/repo_other/pull-requests/2");
  await browser.waitFor(contains("Repository access denied"));
  assert.equal(
    await browser.evaluate(contains("Confirmed architectural conflicts (3)")),
    false
  );
});
test("historical evidence is fetched on selection and remains visibly historical", async () => {
  state.history = true;
  await visit(root + "/pull-requests/2");
  await browser.waitFor(contains("Confirmed architectural conflicts (3)"));
  assert.equal(
    state.calls.some((call) => call.url.includes("/evaluations/")),
    false
  );
  await browser.click("Next");
  await browser.waitFor(
    "document.querySelector('code[title=\"" + "a".repeat(40) + "\"]') !== null"
  );
  await browser.click("Inspect evaluation evidence");
  await browser.waitFor(contains("Historical or stale evidence"));
  assert.ok(
    state.calls.some((call) =>
      call.url.includes("evaluations/historical-run/result")
    )
  );
  await browser.click("Latest observed evaluation");
  await browser.waitFor(contains("Latest evaluation evidence"));
});
test("loading, empty, error and access-denied states do not display a false clean verdict", async () => {
  state.delay = 700;
  await visit(root + "/pull-requests");
  await browser.waitFor(contains("Loading pull requests"));
  await browser.waitFor(contains(actual.detail.pull_request.title));
  state.empty = true;
  await visit(root + "/pull-requests");
  await browser.waitFor(contains("No observed pull requests"));
  for (const status of [503, 403, 401]) {
    state.failure = status;
    await visit(root + "/pull-requests");
    await browser.waitFor(
      status === 401
        ? "location.pathname === '/auth/login'"
        : contains(
            status === 403
              ? "You do not have access"
              : "Unable to load architectural reviews"
          )
    );
  }
});
test("stale current metadata preserves history without presenting old conflicts as a current verdict", async () => {
  state.detail.pull_request.head_sha = "b".repeat(40);
  state.detail.pull_request.freshness = "STALE";
  state.detail.pull_request.evaluation = null;
  state.detail.result = null;
  await visit(root + "/pull-requests/2");
  await browser.waitFor(
    contains("No canonical result is available for the observed revision")
  );
  assert.equal(
    await browser.evaluate(contains("Confirmed architectural conflicts (3)")),
    false
  );
  await browser.click("Inspect evaluation evidence");
  await browser.waitFor(contains("Historical or stale evidence"));
  await browser.waitFor(contains("Confirmed architectural conflicts (3)"));
});
