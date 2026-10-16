import assert from "node:assert/strict";
import { before, beforeEach, after, test } from "node:test";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createServer } from "vite";
import { openBrowser } from "./browser-helper.mjs";

const actual = JSON.parse(
  await readFile(new URL("./fixtures/stage-4-5-case-a.json", import.meta.url))
);
const identities = JSON.parse(
  await readFile(
    new URL("./fixtures/stage-4-5-3-identities.json", import.meta.url)
  )
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
            if (
              url.pathname.startsWith(
                "/repositories/repo_1120610241/knowledge/"
              )
            ) {
              const id = decodeURIComponent(url.pathname.split("/").at(-1));
              const record = identities.knowledge.find(
                (item) => item.id === id
              );
              return record
                ? json({
                    ...record,
                    entities: [],
                    evidence: [],
                    entities_truncated: false,
                    evidence_truncated: false,
                    relationship_basis: "documented_ADR_knowledge",
                  })
                : json({ detail: "Not found" }, 404);
            }
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
                  : current.items || actual.page.items,
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
  state.overview.decisions_needing_attention.items = identities.attention.map(
    (risk) => ({
      ...risk,
      knowledge_resolution: "RESOLVED",
      knowledge: identities.knowledge.find((item) => item.id === risk.identity),
    })
  );
  for (const section of ["review_activity", "recent_pr_reviews"]) {
    state.overview[section].items = state.overview[section].items.map(
      (review) => ({
        ...review,
        title: actual.detail.pull_request.title,
        observed_at: actual.detail.pull_request.observed_at,
      })
    );
  }
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
async function screenshot(name, stage = "stage_4_5") {
  if (stage === "stage_4_5_1") {
    const overflow = await browser.evaluate(
      "({ width: innerWidth, page: document.documentElement.scrollWidth, elements: Array.from(document.querySelectorAll('body *')).filter(e=>e.getBoundingClientRect().right > document.documentElement.clientWidth && getComputedStyle(e).position !== 'fixed').slice(0,12).map(e=>({tag:e.tagName,classes:e.className,right:e.getBoundingClientRect().right})) })"
    );
    assert.ok(overflow.page <= overflow.width, JSON.stringify(overflow));
  }
  if (process.env.CAPTURE_REVIEW_SCREENSHOTS !== "1") return;
  const directory = new URL(`../../backend/data/${stage}/`, import.meta.url);
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
  assert.equal(await browser.evaluate(contains("3 confirmed")), true);
  await link("#2 ");
  await browser.waitFor(contains("Key architectural findings"));
  await browser.click("Findings");
  await browser.waitFor(contains("Confirmed conflicts"));
  assert.equal(await browser.evaluate(contains("Requires review")), true);
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
  await browser.click("Evidence");
  await browser.waitFor(contains("No verified public source link"));
  await browser.evaluate(
    "Array.from(document.querySelectorAll('summary')).find(e=>e.textContent.includes('Expand full source excerpts')).click()"
  );
  await browser.waitFor(contains("Merge base"));
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

test("compact PR queue preserves Case A identity, publication, responsive layout and read-only navigation", async () => {
  await visit(root + "/pull-requests");
  await browser.waitFor(contains(actual.detail.pull_request.title));
  const table = "document.querySelector('.pr-queue')";
  assert.equal(
    await browser.evaluate(`${table}.querySelectorAll('tbody tr').length`),
    1
  );
  for (const text of [
    actual.detail.pull_request.author,
    "Confirmed conflicts",
    "3 confirmed",
    "1 requiring review",
    "Partial",
    "Matches observed revision",
  ])
    assert.equal(
      await browser.evaluate(
        `${table}.innerText.includes(${JSON.stringify(text)})`
      ),
      true,
      text
    );
  assert.equal(
    await browser.evaluate(
      `${table}.querySelector('.queue-review .queue-secondary').title.includes('not a live branch check')`
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      `${table}.querySelector('.queue-links a:last-child').href`
    ),
    "https://github.com/Aditya61101/claimIQ/runs/116733676654"
  );
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('button')).some(b => b.textContent === 'Refresh')"
    ),
    false
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('[data-availability], pre').length"
    ),
    0
  );
  assert.equal(
    await browser.evaluate(
      `${table}.querySelector('table').getBoundingClientRect().width <= ${table}.getBoundingClientRect().width`
    ),
    true
  );
  for (const width of [1024, 768, 390, 320]) {
    await browser.call("Emulation.setDeviceMetricsOverride", {
      width,
      height: 844,
      deviceScaleFactor: 1,
      mobile: width < 768,
    });
    assert.equal(
      await browser.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
      ),
      true,
      `overflow at ${width}`
    );
    assert.equal(
      await browser.evaluate(
        `${table}.querySelector('tbody tr').getBoundingClientRect().width <= innerWidth`
      ),
      true
    );
  }
  assert.equal(
    state.calls.filter((call) => /\/pull-requests\?/.test(call.url)).length,
    1
  );
  await link("#2 ");
  await browser.waitFor(contains("Key architectural findings"));
  assert.ok(state.calls.every((call) => call.method === "GET"));
  assert.equal(
    state.calls.some((call) => /continue|publish/.test(call.url)),
    false
  );
});

test("Overview resolves actual knowledge identities, keeps two separate requirements, and targets exact records", async () => {
  await visit(root + "/overview");
  const confirmed = identities.attention.find(
    (item) => item.kind === "DECISION" && item.classification === "CONFIRMED"
  );
  const decision = identities.knowledge.find(
    (item) => item.id === confirmed.identity
  );
  await browser.waitFor(contains(decision.title));
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('[data-risk-kind=DECISION]').length"
    ),
    2
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('[data-risk-kind=REQUIREMENT]').length"
    ),
    2
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('#attention-title').closest('section').querySelector('details').open"
    ),
    false
  );
  assert.equal(
    await browser.evaluate(
      contains("2 other affected architectural requirements")
    ),
    true
  );
  assert.equal(
    await browser.evaluate(contains("Architectural review activity")),
    false
  );
  assert.equal(
    await browser.evaluate("document.querySelectorAll('main pre').length"),
    0
  );
  assert.deepEqual(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('[data-availability]')).map(e => e.textContent)"
    ),
    ["74", "2", "1", "7"]
  );
  await browser.evaluate(
    "document.querySelector('#attention-title').closest('section').querySelector('summary').click()"
  );
  for (const requirement of identities.knowledge.filter(
    (item) => item.kind === "constraint"
  ))
    assert.equal(
      await browser.evaluate(contains(requirement.description)),
      true
    );
  for (const width of [1440, 768, 390, 320]) {
    await browser.call("Emulation.setDeviceMetricsOverride", {
      width,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: width < 768,
    });
    assert.equal(
      await browser.evaluate(
        "document.documentElement.scrollWidth <= innerWidth"
      ),
      true,
      `Overview overflow at ${width}`
    );
  }
  await browser.evaluate(
    `document.querySelector('a[href*="record=${confirmed.identity}"]').click()`
  );
  await browser.waitFor(
    "document.querySelector('[data-slot=knowledge-detail-modal]')?.innerText.includes(" +
      JSON.stringify(decision.title) +
      ")"
  );
  assert.ok(
    state.calls.some((call) =>
      call.url.includes("/knowledge/" + confirmed.identity)
    )
  );
  await browser.click("Close architectural knowledge");
  await browser.waitFor(
    "!document.querySelector('[data-slot=knowledge-detail-modal]')"
  );
  assert.equal(
    await browser.evaluate(
      "new URLSearchParams(location.search).has('record')"
    ),
    false
  );
  await visit(root + "/overview");
  await browser.waitFor(contains(decision.title));
  await link("Review PR #2");
  await browser.waitFor(contains("Key architectural findings"));
  assert.ok(state.calls.every((call) => call.method === "GET"));
});
test("PR pagination loads a separate offset and repository isolation blocks foreign routes", async () => {
  await visit(root + "/pull-requests");
  await browser.waitFor(contains(actual.detail.pull_request.title));
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Previous').disabled"
    ),
    true
  );
  await browser.click("Next");
  await browser.waitFor(contains("Previous PR"));
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Next').disabled"
    ),
    true
  );
  assert.equal(await browser.evaluate(contains("21–21 of 21")), true);
  await browser.click("Previous");
  await browser.waitFor(contains(actual.detail.pull_request.title));
  assert.ok(state.calls.some((call) => call.url.includes("offset=20")));
  await visit("/projects/repo_other/pull-requests/2");
  await browser.waitFor(contains("Repository access denied"));
  assert.equal(
    await browser.evaluate(contains("Key architectural findings")),
    false
  );
});
test("historical evidence is fetched on selection and remains visibly historical", async () => {
  state.history = true;
  await visit(root + "/pull-requests/2");
  await browser.waitFor(contains("Key architectural findings"));
  assert.equal(
    state.calls.some((call) => call.url.includes("/evaluations/")),
    false
  );
  await browser.click("History");
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
  await browser.click("Return to observed evaluation");
  await browser.waitFor(contains("Key architectural findings"));
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
    await browser.evaluate(contains("Key architectural findings")),
    false
  );
  await browser.click("History");
  await browser.click("Inspect evaluation evidence");
  await browser.waitFor(contains("Historical or stale evidence"));
  await browser.waitFor(contains("Key architectural findings"));
});

test("Stage 4.5.1 five tabs, finding navigation, evidence provenance and responsive layouts", async () => {
  await visit(root + "/pull-requests/2");
  await browser.waitFor(contains("Key architectural findings"));
  assert.deepEqual(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('[role=tab]')).map(e=>e.textContent)"
    ),
    ["Overview", "Findings", "Evidence", "Coverage", "History"]
  );
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('[role=tab]')).every(e => document.getElementById(e.getAttribute('aria-controls')) !== null)"
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('main').classList.contains('max-w-6xl')"
    ),
    false
  );
  assert.equal(
    await browser.evaluate("document.querySelectorAll('main aside').length"),
    0
  );
  await screenshot("overview.png", "stage_4_5_1");
  await browser.evaluate(
    "document.querySelector('[role=tabpanel]:not([hidden]) button').click()"
  );
  await browser.waitFor(
    "document.querySelector('[role=tab][aria-selected=true]').textContent === 'Findings'"
  );
  await browser.click("Overview");
  await browser.waitFor(contains("Key architectural findings"));
  const id = actual.detail.result.findings.find(
    (finding) => finding.disposition === "SUPPORTED_CONFLICT"
  ).finding_id;
  await browser.evaluate(
    `document.querySelector('[data-finding-id="${id}"]').click()`
  );
  await browser.waitFor(
    `document.activeElement.id === ${JSON.stringify("finding-" + id)}`
  );
  assert.equal(
    await browser.evaluate(contains("Unverified / rejected allegations (3)")),
    true
  );
  await browser.evaluate("window.scrollTo(0,0)");
  await screenshot("findings.png", "stage_4_5_1");
  await browser.click("Evidence");
  await browser.waitFor(contains("Original architectural evidence"));
  const diff = await browser.evaluate(
    "document.querySelector('[data-testid=focused-diff]').innerText"
  );
  assert.match(diff, /58/);
  assert.match(diff, /if statuses == \{"VERIFIED"\}:/);
  assert.match(diff, /if "VERIFIED" in statuses:/);
  assert.equal(
    await browser.evaluate(
      "document.querySelector('summary').closest('details').open"
    ),
    false
  );
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('pre')).some(e=>e.textContent.includes('schema_version'))"
    ),
    false
  );
  await browser.evaluate("window.scrollTo(0,0)");
  await screenshot("evidence.png", "stage_4_5_1");
  await browser.evaluate(
    "Array.from(document.querySelectorAll('summary')).find(e=>e.textContent === 'Canonical evaluation evidence (technical inspection)').click()"
  );
  await browser.waitFor(
    "Array.from(document.querySelectorAll('pre')).some(e=>e.textContent.includes('schema_version'))"
  );
  const canonical = await browser.evaluate(
    "Array.from(document.querySelectorAll('pre')).find(e=>e.textContent.includes('schema_version')).textContent"
  );
  assert.deepEqual(JSON.parse(canonical), actual.detail.result);
  assert.equal(
    await browser.evaluate(
      "Array.from(document.querySelectorAll('button')).some(e=>e.textContent === 'Copy JSON')"
    ),
    true
  );
  await browser.click("Coverage");
  await browser.waitFor(contains("Job lifecycle: Deferred"));
  assert.equal(
    await browser.evaluate(contains("Evaluator execution: Completed")),
    true
  );
  assert.equal(
    await browser.evaluate(contains("Some evaluation work is deferred")),
    true
  );
  await browser.evaluate("window.scrollTo(0,0)");
  await screenshot("coverage.png", "stage_4_5_1");
  await browser.click("History");
  await browser.waitFor(contains("Evaluation history"));
  await browser.evaluate("window.scrollTo(0,0)");
  await screenshot("history.png", "stage_4_5_1");
  await browser.evaluate(
    "document.querySelector('#review-tab-History').focus()"
  );
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "Home",
    code: "Home",
  });
  await browser.waitFor(
    "document.activeElement.id === 'review-tab-Overview' && document.activeElement.getAttribute('aria-selected') === 'true'"
  );
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "ArrowRight",
    code: "ArrowRight",
  });
  await browser.waitFor("document.activeElement.id === 'review-tab-Findings'");
  await browser.click("Overview");
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  await browser.evaluate("window.scrollTo(0,0)");
  assert.equal(
    await browser.evaluate(
      "document.documentElement.scrollWidth <= window.innerWidth"
    ),
    true
  );
  await screenshot("mobile.png", "stage_4_5_1");
  for (const tab of ["Findings", "Evidence", "Coverage", "History"]) {
    await browser.click(tab);
    assert.equal(
      await browser.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
      ),
      true,
      tab
    );
  }
  assert.equal(
    state.calls.filter((call) => call.url.includes("/pull-requests/2")).length,
    1
  );
  assert.ok(state.calls.every((call) => call.method === "GET"));
});
