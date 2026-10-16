import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";

const fixture = JSON.parse(
  await readFile(new URL("./fixtures/stage-4-5-case-a.json", import.meta.url))
);
let server,
  parse,
  presentation,
  service,
  client,
  Evidence,
  Findings,
  CoveragePanel,
  Summary,
  Risks,
  Queue;
before(async () => {
  globalThis.localStorage = { getItem: () => "test-token", removeItem() {} };
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  parse = await server.ssrLoadModule("/src/lib/evaluation-contract.ts");
  presentation = await server.ssrLoadModule("/src/lib/review-presentation.ts");
  ({ pullRequestService: service } = await server.ssrLoadModule(
    "/src/api/pull-request-service.ts"
  ));
  ({ apiClient: client } = await server.ssrLoadModule(
    "/src/lib/api-client.ts"
  ));
  ({
    ReviewEvidence: Evidence,
    ReviewFindings: Findings,
    ReviewCoverage: CoveragePanel,
  } = await server.ssrLoadModule(
    "/src/components/repositories/pr-review-evidence.tsx"
  ));
  ({ PRSummary: Summary } = await server.ssrLoadModule(
    "/src/components/repositories/review-summary.tsx"
  ));
  ({ DecisionRisks: Risks } = await server.ssrLoadModule(
    "/src/components/repositories/decision-risks.tsx"
  ));
  ({ PullRequestTable: Queue } = await server.ssrLoadModule(
    "/src/components/repositories/pull-request-table.tsx"
  ));
});

test("PR queue distinguishes incomplete, missing, running and stale evaluations and never invents a Check link", () => {
  const pr = fixture.detail.pull_request;
  const before = JSON.stringify(pr);
  const display = (changes = {}, evaluation = pr.evaluation) =>
    render(Queue, { items: [{ ...pr, ...changes, evaluation }] });
  const html = display();
  assert.match(html, /3 confirmed/);
  assert.match(html, /1 requiring review/);
  assert.match(html, /Partial/);
  assert.match(html, /116733676654/);
  assert.match(html, /not a live branch check/);
  assert.doesNotMatch(html, /rejected records|<pre|View pull request/);
  for (const state of ["PENDING", "FAILED", "DISABLED", "RETRY", "SKIPPED"]) {
    const unpublished = display(
      {},
      {
        ...pr.evaluation,
        publication: { ...pr.evaluation.publication, status: state },
      }
    );
    assert.doesNotMatch(unpublished, /116733676654|GitHub Check/);
    assert.match(unpublished, /Confirmed conflicts/);
  }
  const clean = {
    ...pr.evaluation,
    confirmed_conflicts: 0,
    review_required_findings: 0,
    outcome: "NO_CONFLICTS_FOUND",
    coverage: {
      ...pr.evaluation.coverage,
      scope: "COMPLETE_FOR_RETAINED_SCOPE",
    },
  };
  assert.doesNotMatch(display({}, clean), /incomplete review/);
  assert.match(
    display(
      {},
      { ...clean, coverage: { ...clean.coverage, scope: "PARTIAL" } }
    ),
    /incomplete review/
  );
  assert.match(display({}, { ...clean, coverage: null }), /incomplete review/);
  assert.match(
    display({}, { ...clean, execution: "PARTIAL" }),
    /incomplete review/
  );
  assert.match(
    display({}, { ...clean, outcome: "REVIEW_REQUIRED" }),
    /Requires review/
  );
  for (const scope of ["PARTIAL", "UNASSESSABLE"])
    assert.doesNotMatch(
      display({}, { ...clean, coverage: { ...clean.coverage, scope } }),
      /queue-neutral/
    );
  for (const status of ["QUEUED", "RUNNING", "FAILED"]) {
    const pending = display({}, { ...clean, status, outcome: null });
    assert.match(
      pending,
      new RegExp(
        {
          QUEUED: "Evaluation queued",
          RUNNING: "Evaluating",
          FAILED: "Evaluation unavailable",
        }[status]
      )
    );
    assert.doesNotMatch(pending, /No confirmed conflicts/);
  }
  const missing = display(
    { title: null, author: null, freshness: "UNKNOWN", observed_at: null },
    null
  );
  assert.match(missing, /Title unavailable/);
  assert.match(missing, /Author unavailable/);
  assert.match(missing, /Not evaluated/);
  assert.doesNotMatch(missing, /No confirmed conflicts|116733676654/);
  assert.match(display({ freshness: "STALE" }, null), /Stale analysis/);
  assert.equal(JSON.stringify(pr), before);
});
after(async () => {
  await server?.close();
  delete globalThis.localStorage;
});
const render = (Component, props) =>
  renderToStaticMarkup(
    createElement(MemoryRouter, null, createElement(Component, props))
  );

test("actual persisted Case A satisfies generated API contracts and retains canonical evidence", () => {
  const copy = JSON.stringify(fixture);
  assert.equal(parse.parseDetail(fixture.detail), fixture.detail);
  assert.equal(parse.parsePage(fixture.page), fixture.page);
  assert.equal(parse.parseResult(fixture.detail.result), fixture.detail.result);
  assert.equal(JSON.stringify(fixture), copy);
  const groups = presentation.findingGroups(fixture.detail.result);
  assert.equal(groups.confirmed.length, 3);
  assert.equal(groups.review.length, 1);
  assert.equal(fixture.detail.result.coverage.uncertain, 6);
  assert.equal(fixture.detail.result.rejected_findings.length, 3);
});
test("malformed responses fail clearly, including invalid counts, coverage and identity shapes", () => {
  for (const patch of [
    { total: -1 },
    { items: [{}] },
    { items: [{ ...fixture.page.items[0], freshness: "made-up" }] },
  ]) {
    assert.throws(
      () => parse.parsePage({ ...fixture.page, ...patch }),
      /incompatible/
    );
  }
  assert.throws(
    () =>
      parse.parseResult({
        ...fixture.detail.result,
        coverage: { ...fixture.detail.result.coverage, assessed: -1 },
      }),
    /incompatible/
  );
});
test("GET clients encode scoped identities, paginate, propagate abort signals and reject foreign data", async () => {
  const calls = [];
  const controller = new AbortController();
  client.defaults.adapter = async (config) => {
    calls.push(config);
    return {
      data: fixture.page,
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
  };
  await service.list("repo_1120610241", 20, controller.signal);
  assert.equal(calls[0].url, "/repositories/repo_1120610241/pull-requests");
  assert.deepEqual(calls[0].params, { limit: 20, offset: 20 });
  assert.equal(calls[0].signal, controller.signal);
  assert.equal(calls[0].headers.Authorization, "Bearer test-token");
  await assert.rejects(service.list("repo_other"), /mismatch/);
  await assert.rejects(service.list("../escape"), /Invalid/);
  await assert.rejects(service.detail("repo_1120610241", -1), /Invalid/);
  client.defaults.adapter = async (config) => ({
    data: fixture.detail,
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  });
  await service.detail("repo_1120610241", 2);
  await assert.rejects(service.detail("repo_1120610241", 3), /mismatch/);
  client.defaults.adapter = async (config) => ({
    data: fixture.detail.result,
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  });
  assert.deepEqual(
    await service.result("repo_1120610241", "historical-id"),
    fixture.detail.result
  );
  await assert.rejects(
    service.result("repo_other", "historical-id"),
    /mismatch/
  );
  assert.ok(calls.every((call) => call.method === "get"));
});
test("Case A evidence separates confirmed, unresolved and rejected records and exposes original comparisons", () => {
  const props = {
    result: fixture.detail.result,
    fullName: "Aditya61101/claimIQ",
  };
  const html =
    render(Findings, props) +
    render(Evidence, props) +
    render(CoveragePanel, { ...props, lifecycle: "DEFERRED" });
  assert.match(html, /Confirmed conflicts/);
  assert.match(html, /Requires review/);
  assert.match(html, /Unverified \/ rejected allegations \(3\)/);
  assert.match(html, /Uncertain/);
  assert.match(html, /Code changes and revision comparison/);
  assert.match(html, /Independent challenger verification/);
  assert.match(html, /Original architectural evidence/);
  assert.match(html, /not confirmed conflicts/);
  assert.match(
    html,
    /could not validate the required original-source evidence/
  );
  assert.match(html, /Canonical evaluation evidence/);
});
test("revision freshness, missing metadata and partial verdicts are explicit", () => {
  const pr = fixture.detail.pull_request;
  const html = render(Summary, { pr });
  assert.match(html, /Matches observed revision/);
  assert.match(html, /116733676654/);
  assert.match(html, /not a live branch check/);
  assert.match(html, /Partial/);
  const stale = render(Summary, {
    pr: {
      ...pr,
      freshness: "STALE",
      evaluation: null,
      author: null,
      title: null,
    },
  });
  assert.match(stale, /Stale analysis/);
  assert.match(stale, /Author unavailable/);
  assert.match(stale, /No evaluation for the observed revision/);
  assert.doesNotMatch(stale, /Architectural conflicts detected/);
});
test("decision and unmapped requirement associations remain separate and never infer extra decisions", () => {
  const risks = [
    {
      identity: "d",
      kind: "DECISION",
      classification: "CONFIRMED",
      pr_numbers: [2],
      finding_ids: ["a", "b"],
      adr_ids: [],
    },
    {
      identity: "r",
      kind: "REQUIREMENT",
      classification: "POTENTIAL",
      pr_numbers: [2],
      finding_ids: ["c"],
      adr_ids: [],
    },
  ];
  const html = render(Risks, {
    risks,
    repositoryId: "repo_1120610241",
    total: 3,
  });
  assert.match(html, /Confirmed conflict/);
  assert.match(html, /Potential risk/);
  assert.match(html, /without an explicit decision mapping/);
  assert.match(html, /Showing 2 of 3/);
  assert.match(html, /repo_1120610241\/pull-requests\/2/);
});
test("source links require exact immutable provenance and escape unsafe display text", () => {
  assert.equal(
    presentation.checkUrl("Aditya61101/claimIQ", "116733676654"),
    "https://github.com/Aditya61101/claimIQ/runs/116733676654"
  );
  assert.equal(
    presentation.codeUrl("evil/../x", "a".repeat(40), {
      path: "a.py",
      start_line: 1,
      end_line: 2,
    }),
    null
  );
  assert.equal(
    presentation.codeUrl("org/repo", "main", {
      path: "a.py",
      start_line: 1,
      end_line: 2,
    }),
    null
  );
  assert.equal(
    presentation.codeUrl("org/repo", "a".repeat(40), {
      path: "../x",
      start_line: 1,
      end_line: 2,
    }),
    null
  );
  assert.equal(presentation.checkUrl("org/repo", "javascript:alert(1)"), null);
  const html = render(Summary, {
    pr: { ...fixture.detail.pull_request, title: "<script>alert(1)</script>" },
  });
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});
