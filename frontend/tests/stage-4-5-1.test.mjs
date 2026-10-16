import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";

const fixture = JSON.parse(
  await readFile(new URL("./fixtures/stage-4-5-case-a.json", import.meta.url))
);
const result = fixture.detail.result;
let server, model, components, page;
before(async () => {
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  model = await server.ssrLoadModule("/src/lib/pr-review-model.ts");
  components = await server.ssrLoadModule(
    "/src/components/repositories/pr-review-evidence.tsx"
  );
  page = await server.ssrLoadModule(
    "/src/pages/repository-pull-request-detail.tsx"
  );
});
after(async () => {
  await server?.close();
});
const render = (component, props) =>
  renderToStaticMarkup(
    createElement(MemoryRouter, null, createElement(component, props))
  );

test("focused diff uses exact persisted base/head source, text and line 58 without changing the canonical result", () => {
  const before = JSON.stringify(result);
  const comparisons = model.focusedComparisons(result);
  assert.equal(comparisons.length, 1);
  const diff = comparisons[0];
  assert.equal(diff.before.origin, "TARGET_BASE");
  assert.equal(diff.before.commit_sha, result.revision.base_sha);
  assert.equal(diff.after.commit_sha, result.revision.head_sha);
  assert.deepEqual(diff.changedBefore, ['    if statuses == {"VERIFIED"}:']);
  assert.deepEqual(diff.changedAfter, ['    if "VERIFIED" in statuses:']);
  assert.equal(diff.beforeLines.find((line) => line.changed).line, 58);
  assert.equal(diff.afterLines.find((line) => line.changed).line, 58);
  assert.equal(JSON.stringify(result), before);
});
test("missing, mismatched, unlocated and unrelated source data never synthesize a diff", () => {
  for (const change of [
    (copy) => {
      copy.evidence = copy.evidence.filter(
        (source) => source.origin !== "TARGET_BASE"
      );
    },
    (copy) => {
      copy.revision.head_sha = "a".repeat(40);
    },
    (copy) => {
      copy.evidence.forEach((source) => {
        if (source.origin === "PR_HEAD") source.placement = "UNLOCATED";
      });
    },
    (copy) => {
      copy.findings = [];
    },
    (copy) => {
      copy.evidence.forEach((source) => {
        if (source.origin === "PR_HEAD") source.location.end_line++;
      });
    },
  ]) {
    const copy = structuredClone(result);
    change(copy);
    assert.equal(model.focusedComparisons(copy).length, 0);
    assert.match(
      render(components.FocusedDiff, {
        result: copy,
        fullName: "Aditya61101/claimIQ",
      }),
      /reliable focused diff is unavailable/
    );
  }
});
test("reliably recorded shared changes are distinguished from merely similar prose", () => {
  const confirmed = result.findings.filter(
    (finding) => finding.disposition === "SUPPORTED_CONFLICT"
  );
  assert.equal(model.sharedChange(result, confirmed), true);
  const copy = structuredClone(result);
  copy.plan.forEach((unit, index) => {
    unit.delta_ids = [`unique-${index}`];
  });
  assert.equal(model.sharedChange(copy, confirmed), false);
});
test("identical source spans are presented once while every original finding association remains", () => {
  const before = JSON.stringify(result);
  const groups = model.evidenceGroups(result);
  const inputs = result.findings.flatMap(model.findingCitations);
  assert.ok(groups.length < inputs.length);
  assert.ok(groups.some((group) => group.findingIds.length > 1));
  for (const finding of result.findings)
    assert.ok(
      groups.some((group) => group.findingIds.includes(finding.finding_id))
    );
  for (const group of groups)
    assert.ok(group.source.excerpt.includes(group.citation.quote));
  assert.equal(JSON.stringify(result), before);
});
test("invalid challenger supported verdicts never expose a witness as independently accepted", () => {
  const unresolved = structuredClone(
    result.findings.find((finding) => finding.disposition === "NEEDS_REVIEW")
  );
  unresolved.review.tested_witness =
    "UNVALIDATED_SCENARIO_NEVER_PRESENT_AS_VERIFIED";
  assert.equal(model.acceptedReview(unresolved), false);
  const html = render(components.Verification, {
    result: { ...result, findings: [unresolved] },
  });
  assert.match(html, /Unresolved \/ not accepted as confirmation/);
  assert.doesNotMatch(html, /UNVALIDATED_SCENARIO/);
  assert.doesNotMatch(html, /Conflict independently supported/);
  const refuted = {
    ...unresolved,
    disposition: "REFUTED",
    review_error: null,
    review_rejection: null,
    review: {
      ...unresolved.review,
      verdict: "REFUTED",
      explanation: "Validated counterevidence defeats the original allegation.",
    },
  };
  assert.equal(model.acceptedChallenger(refuted), true);
  const refutedHtml = render(components.Verification, {
    result: { ...result, findings: [refuted] },
  });
  assert.match(refutedHtml, /Validated counterevidence defeats/);
  assert.doesNotMatch(refutedHtml, /Unresolved \/ not accepted/);
});
test("overview, finding classes, rejected records and deferred/partial state remain distinct", () => {
  const overview = render(components.ReviewOverview, {
    result,
    fullName: "Aditya61101/claimIQ",
    open() {},
  });
  assert.match(overview, /Key architectural findings/);
  assert.match(overview, /same recorded code change/);
  assert.doesNotMatch(overview, /WITNESS_ORIGINAL_SOURCE_NOT_REVIEWED/);
  const coverage = render(components.ReviewCoverage, {
    result,
    lifecycle: "DEFERRED",
    deferred: fixture.detail.deferred,
  });
  assert.match(coverage, /Job lifecycle: Deferred/);
  assert.match(coverage, /Evaluator execution: Completed/);
  assert.match(coverage, /not a completely assessed PR/);
  assert.match(coverage, /Some evaluation work is deferred/);
  assert.doesNotMatch(coverage, /CONSTRAINT_LIMIT/);
});
test("verdict and counts come from the selected result rather than ClaimIQ-specific copy", () => {
  const copy = structuredClone(result);
  copy.findings = [];
  copy.outcome = "REVIEW_REQUIRED";
  copy.coverage.uncertain = 17;
  const html = render(page.VerdictPanel, {
    result: copy,
    evaluation: null,
    selected: true,
  });
  assert.match(html, /Selected evaluation/);
  assert.match(html, /Further architectural review required/);
  assert.match(html, /17 uncertain units/);
  assert.doesNotMatch(html, /3 supported governing-rule findings/);
});
test("technical records are retained but not serialized into the initial review DOM", () => {
  const html = render(page.PRReview, {
    detail: fixture.detail,
    result,
    selected: null,
    select() {},
    offset: 0,
    onPage() {},
  });
  assert.match(html, /Overview/);
  assert.doesNotMatch(
    html,
    /&quot;schema_version&quot;|&quot;input_tokens&quot;/
  );
  const evidence = render(components.ReviewEvidence, {
    result,
    fullName: "Aditya61101/claimIQ",
    associations: fixture.detail.governing_rules,
  });
  assert.match(evidence, /Canonical evaluation evidence/);
  assert.match(evidence, /Governing decision and requirement associations/);
  assert.doesNotMatch(evidence, /&quot;schema_version&quot;/);
  assert.match(evidence, /No verified public source link/);
  assert.doesNotMatch(evidence, /href="http:\/\/localhost/);
});
