import type { ReactNode } from "react";
import {
  AlertCircle,
  ArrowRight,
  BookOpen,
  FileCode2,
  ShieldCheck,
} from "lucide-react";
import type {
  EvaluationResult,
  Finding,
  GoverningRule,
} from "@/types/evaluation.generated";
import {
  acceptedReview,
  acceptedChallenger,
  adrNames,
  evidenceGroups,
  focusedComparisons,
  limitationSummary,
  readable,
  revisionSources,
  sharedChange,
} from "@/lib/pr-review-model";
import {
  citationSource,
  citationUrl,
  codeUrl,
  findingGroups,
  label,
  reviewReason,
} from "@/lib/review-presentation";
import { TechnicalDetails } from "@/components/repositories/technical-details";

export type ReviewTab =
  "Overview" | "Findings" | "Evidence" | "Coverage" | "History";
export function ReviewLink({
  href,
  children,
}: {
  href: string | null;
  children: ReactNode;
}) {
  return href ? (
    <a
      className="review-link inline-flex items-center gap-1"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
    >
      {children}
      <span className="sr-only"> (opens in a new tab)</span>
    </a>
  ) : null;
}
export function ReviewCard({
  title,
  icon,
  children,
  action,
}: {
  title: string;
  icon?: ReactNode;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="review-card space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="review-heading">
          {icon}
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}
export function FocusedDiff({
  result,
  fullName,
  compact = false,
}: {
  result: EvaluationResult;
  fullName: string;
  compact?: boolean;
}) {
  const confirmed = result.findings.filter(
    (finding) => finding.disposition === "SUPPORTED_CONFLICT"
  );
  const comparisons = focusedComparisons(
    result,
    compact && confirmed.length ? confirmed : result.findings
  );
  return (
    <div className="space-y-4">
      {comparisons.length ? (
        (compact ? comparisons.slice(0, 1) : comparisons).map((comparison) => (
          <div key={comparison.after.evidence_id} className="space-y-3">
            <p className="review-subtitle break-all">{comparison.after.path}</p>
            <div className="review-diff" data-testid="focused-diff">
              {(["before", "after"] as const).map((side) => {
                const source = comparison[side];
                return (
                  <section key={side} className="min-w-0">
                    <div className="review-code-header">
                      <span>
                        {side === "before" ? "Base" : "PR head"}{" "}
                        <code title={source.commit_sha ?? undefined}>
                          ({source.commit_sha?.slice(0, 7)})
                        </code>
                      </span>
                      <ReviewLink
                        href={codeUrl(
                          fullName,
                          source.commit_sha,
                          source.location
                        )}
                      >
                        View source
                      </ReviewLink>
                    </div>
                    <pre
                      className="review-code-lines"
                      aria-label={
                        side === "before" ? "Base code" : "PR head code"
                      }
                    >
                      {comparison[`${side}Lines`].map((line) => (
                        <div
                          key={line.line}
                          className={`review-code-row ${line.changed ? (side === "before" ? "removed" : "added") : ""}`}
                        >
                          <span className="review-line-no">{line.line}</span>
                          <code>
                            {line.changed
                              ? side === "before"
                                ? "− "
                                : "+ "
                              : "  "}
                            {line.text}
                          </code>
                        </div>
                      ))}
                    </pre>
                  </section>
                );
              })}
            </div>
            <p className="review-subtitle">
              Exact comparison of persisted revision-specific excerpts;
              unchanged outer lines are collapsed.
              {comparison.truncated
                ? " Preview shortened; expand the full source excerpts in Evidence for the complete retained text."
                : ""}
            </p>
          </div>
        ))
      ) : (
        <p className="review-subtitle">
          A reliable focused diff is unavailable: matching exact base/head
          source windows were not retained. Verified source links and original
          excerpts remain available below; no changes have been synthesized.
        </p>
      )}
    </div>
  );
}
function codeLink(
  result: EvaluationResult,
  finding: Finding,
  fullName: string
) {
  for (const citation of finding.citations) {
    const source = citationSource(result, citation);
    if (source?.kind === "CODE" && source.origin === "PR_HEAD") {
      const url = citationUrl(fullName, source, citation);
      if (url) return url;
    }
  }
  return null;
}
export function Verification({
  result,
  compact = false,
  onEvidence,
}: {
  result: EvaluationResult;
  compact?: boolean;
  onEvidence?: () => void;
}) {
  const confirmed = result.findings.filter(acceptedReview);
  const findings = compact ? confirmed.slice(0, 1) : result.findings;
  return (
    <div className="space-y-4">
      {findings.map((finding, index) => (
        <article
          key={finding.finding_id}
          className={compact ? "space-y-3" : "space-y-4 border-t pt-4"}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-medium">
              {compact
                ? "Accepted independent review"
                : `Finding ${index + 1}: ${readable(finding.claim, 120)}`}
            </h3>
            <span
              className={`review-badge ${acceptedReview(finding) ? "review-danger" : acceptedChallenger(finding) ? "" : "review-caution"}`}
            >
              {acceptedReview(finding)
                ? "Conflict independently supported"
                : acceptedChallenger(finding)
                  ? "Refuted"
                  : "Unresolved / not accepted as confirmation"}
            </span>
          </div>
          {acceptedChallenger(finding) && finding.review ? (
            <>
              <div className="review-witness">
                <div className="space-y-2">
                  <h4 className="text-xs font-semibold text-muted-foreground">
                    Tested witness
                  </h4>
                  <p className="text-sm leading-relaxed">
                    {readable(finding.review.tested_witness, 500)}
                  </p>
                </div>
                <div className="space-y-2">
                  <h4 className="text-xs font-semibold text-muted-foreground">
                    Observed behavior
                  </h4>
                  <p className="text-sm leading-relaxed">
                    PR head:{" "}
                    {label(
                      finding.review.revisions.find(
                        (revision) => revision.revision === "PR_HEAD"
                      )?.behavior
                    )}
                    .{" "}
                    {readable(
                      acceptedReview(finding)
                        ? (finding.candidate?.mechanism ?? finding.claim)
                        : finding.review.explanation,
                      310
                    )}
                  </p>
                </div>
                <div className="space-y-2">
                  <h4 className="text-xs font-semibold text-muted-foreground">
                    Governing requirement
                  </h4>
                  <p className="text-sm leading-relaxed">
                    {readable(
                      finding.candidate?.rule.statement ??
                        "See original architectural evidence.",
                      310
                    )}
                  </p>
                </div>
              </div>
              <details className="border-t pt-2">
                <summary className="review-subtitle">
                  Full challenger reasoning and source references
                </summary>
                <p className="py-3 leading-relaxed">
                  {finding.review.explanation}
                </p>
                <TechnicalDetails
                  title="Original independent challenger decision and references"
                  value={finding.review}
                />
              </details>
            </>
          ) : (
            <p className="review-subtitle">
              {reviewReason(finding.review_error)} A model-proposed supported
              verdict alone is not a validated confirmation. Original review
              records remain available in technical inspection.
            </p>
          )}
          {!compact && (
            <TechnicalDetails
              title="Finding review and proof audit"
              value={{
                finding,
                proofs: result.proofs.filter((proof) =>
                  finding.proof_ids.includes(proof.proof_id)
                ),
              }}
            />
          )}
        </article>
      ))}
      {!findings.length && (
        <p className="review-subtitle">
          No accepted independently supported witness is available for this
          evaluation.
        </p>
      )}
      {compact && (
        <p className="review-subtitle">
          Showing {confirmed.length ? 1 : 0} of {confirmed.length} accepted
          conflict reviews.{" "}
          <button className="review-link" onClick={onEvidence}>
            View all independent evidence →
          </button>
        </p>
      )}
    </div>
  );
}
export function ReviewOverview({
  result,
  fullName,
  open,
}: {
  result: EvaluationResult;
  fullName: string;
  open: (tab: ReviewTab, finding?: string) => void;
}) {
  const confirmed = findingGroups(result).confirmed;
  const sourceFinding = confirmed[0];
  return (
    <div className="space-y-5">
      <ReviewCard
        title="Key architectural findings"
        icon={<AlertCircle className="size-4 text-muted-foreground" />}
        action={
          <button
            className="review-link text-xs"
            onClick={() => open("Findings")}
          >
            View all findings →
          </button>
        }
      >
        {sharedChange(result, confirmed) && (
          <p className="review-subtitle">
            These governing-rule findings share the same recorded code change.
            They remain separate requirements; this does not establish
            independent bugs.
          </p>
        )}
        <div className="divide-y rounded-lg border">
          {confirmed.map((finding, index) => (
            <button
              key={finding.finding_id}
              onClick={() => open("Findings", finding.finding_id)}
              data-finding-id={finding.finding_id}
              className="flex w-full items-start gap-3 p-4 text-left transition-colors hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            >
              <AlertCircle
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-destructive"
              />
              <div className="min-w-0 flex-1 space-y-1">
                <p className="leading-relaxed font-medium">
                  {readable(finding.claim, 115)}
                </p>
                <p className="review-subtitle">
                  {adrNames(result, finding).join(" · ") ||
                    "Governing requirement in original evidence"}
                </p>
              </div>
              <span className="review-badge review-danger hidden sm:inline-flex">
                Confirmed {index + 1}
              </span>
              <ArrowRight
                aria-hidden="true"
                className="mt-1 size-4 shrink-0 text-muted-foreground"
              />
            </button>
          ))}
        </div>
        {!confirmed.length && (
          <p className="review-subtitle">
            No confirmed conflict is recorded. Inspect the Findings and Coverage
            tabs for unresolved evidence and scope.
          </p>
        )}
      </ReviewCard>
      <ReviewCard
        title="Focused code change"
        icon={<FileCode2 className="size-4 text-muted-foreground" />}
        action={
          <button
            className="review-link text-xs"
            onClick={() => open("Evidence")}
          >
            View full comparison →
          </button>
        }
      >
        <FocusedDiff result={result} fullName={fullName} compact />
      </ReviewCard>
      <ReviewCard
        title="Independent challenger verification"
        icon={<ShieldCheck className="size-4 text-muted-foreground" />}
      >
        <Verification
          result={result}
          compact
          onEvidence={() => open("Evidence")}
        />
      </ReviewCard>
      <ReviewCard
        title="Governing decision and original ADR"
        icon={<BookOpen className="size-4 text-muted-foreground" />}
        action={
          <button
            className="review-link text-xs"
            onClick={() => open("Evidence")}
          >
            View original evidence →
          </button>
        }
      >
        {sourceFinding ? (
          <>
            <p className="leading-relaxed">
              {readable(
                sourceFinding.candidate?.rule.statement ?? sourceFinding.claim,
                300
              )}
            </p>
            <p className="review-subtitle">
              {adrNames(result, sourceFinding).join(" · ") ||
                "Source document metadata unavailable"}
            </p>
          </>
        ) : (
          <p className="review-subtitle">
            No confirmed governing requirement is available. Original retained
            sources are accessible in Evidence.
          </p>
        )}
      </ReviewCard>
    </div>
  );
}
export function ReviewFindings({
  result,
  fullName,
  onEvidence,
}: {
  result: EvaluationResult;
  fullName: string;
  onEvidence: () => void;
}) {
  const groups = findingGroups(result);
  const sections = [
    {
      title: "Confirmed conflicts",
      findings: groups.confirmed,
      confirmed: true,
    },
    { title: "Requires review", findings: groups.review, confirmed: false },
    {
      title: "Other architectural observations",
      findings: groups.other,
      confirmed: false,
    },
  ];
  const rejected = new Map<string, EvaluationResult["rejected_findings"]>();
  for (const record of result.rejected_findings) {
    const records = rejected.get(record.reason) ?? [];
    records.push(record);
    rejected.set(record.reason, records);
  }
  return (
    <div className="space-y-7">
      {sections
        .filter(
          (section) =>
            section.title !== "Other architectural observations" ||
            section.findings.length
        )
        .map((section) => (
          <section key={section.title} className="space-y-4">
            <h2 className="review-heading">
              {section.title}{" "}
              <span className="text-muted-foreground">
                ({section.findings.length})
              </span>
            </h2>
            {section.findings.map((finding) => (
              <article
                key={finding.finding_id}
                id={`finding-${finding.finding_id}`}
                tabIndex={-1}
                className={`review-card review-finding space-y-4 ${section.confirmed ? "" : "review-unresolved"}`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <h3 className="min-w-0 flex-1 text-base leading-relaxed font-semibold">
                    {readable(finding.claim, 260)}
                  </h3>
                  <span
                    className={`review-badge ${section.confirmed ? "review-danger" : "review-caution"}`}
                  >
                    {label(finding.disposition)}
                  </span>
                </div>
                <p className="review-subtitle">
                  {adrNames(result, finding).join(" · ") ||
                    "Original source association retained in evidence"}
                </p>
                <dl className="grid gap-4 text-sm md:grid-cols-2">
                  {[
                    ["Governing rule", finding.candidate?.rule.statement],
                    ["Mechanism", finding.candidate?.mechanism],
                    ["Consequence", finding.consequence],
                    ["Suggested action", finding.suggested_action],
                  ]
                    .filter(([, value]) => value)
                    .map(([title, value]) => (
                      <div key={title} className="space-y-1">
                        <dt className="text-xs font-semibold text-muted-foreground">
                          {title}
                        </dt>
                        <dd className="leading-relaxed">
                          {readable(value!, 700)}
                        </dd>
                      </div>
                    ))}
                </dl>
                {finding.review_error && (
                  <p className="review-subtitle">
                    {reviewReason(finding.review_error)}
                  </p>
                )}
                <div className="flex flex-wrap gap-5 text-xs">
                  <ReviewLink href={codeLink(result, finding, fullName)}>
                    View changed code ↗
                  </ReviewLink>
                  <button className="review-link" onClick={onEvidence}>
                    Original evidence and challenger review →
                  </button>
                </div>
                {finding.missing_facts.length > 0 && (
                  <details>
                    <summary className="review-subtitle">
                      Missing evidence
                    </summary>
                    {finding.missing_facts.map((fact) => (
                      <p key={fact.fact_id} className="py-2">
                        {fact.fact} — {fact.relevance}
                      </p>
                    ))}
                  </details>
                )}
                <TechnicalDetails
                  title="Technical finding details"
                  value={{
                    finding,
                    proofs: result.proofs.filter((proof) =>
                      finding.proof_ids.includes(proof.proof_id)
                    ),
                  }}
                />
              </article>
            ))}
            {!section.findings.length && (
              <p className="review-subtitle">
                No findings in this classification.
              </p>
            )}
          </section>
        ))}
      <ReviewCard
        title={`Unverified / rejected allegations (${result.rejected_findings.length})`}
      >
        <p className="review-subtitle">
          These rejected proof records are not confirmed conflicts. Identical
          recorded reasons are grouped for readability; each original record is
          preserved.
        </p>
        {[...rejected].map(([reason, records]) => (
          <div key={reason} className="space-y-2 border-t pt-3">
            <p>
              {records.length} {records.length === 1 ? "record" : "records"}:{" "}
              {reviewReason(reason)}
            </p>
            <TechnicalDetails
              title="Original rejected proof records"
              value={records}
            />
          </div>
        ))}
      </ReviewCard>
    </div>
  );
}
export function ReviewEvidence({
  result,
  fullName,
  associations,
}: {
  result: EvaluationResult;
  fullName: string;
  associations?: GoverningRule[];
}) {
  const groups = evidenceGroups(result);
  const sources = new Map<string, EvaluationResult["evidence"][number]>();
  for (const source of revisionSources(result)) {
    sources.set(source.evidence_id, source);
  }
  return (
    <div className="space-y-5">
      <ReviewCard
        title="Code changes and revision comparison"
        icon={<FileCode2 className="size-4" />}
      >
        <FocusedDiff result={result} fullName={fullName} />
        <details className="border-t pt-2">
          <summary className="review-subtitle">
            Expand full source excerpts: base, merge base and PR head
          </summary>
          <div className="mt-3 grid gap-4 lg:grid-cols-3">
            {[...sources.values()]
              .sort(
                (a, b) =>
                  (a.path ?? "").localeCompare(b.path ?? "") ||
                  ["TARGET_BASE", "MERGE_BASE", "PR_HEAD"].indexOf(a.origin) -
                    ["TARGET_BASE", "MERGE_BASE", "PR_HEAD"].indexOf(
                      b.origin
                    ) ||
                  (a.location?.start_line ?? 0) - (b.location?.start_line ?? 0)
              )
              .map((source) => (
                <figure key={source.evidence_id} className="review-quote">
                  <figcaption className="review-code-header">
                    <span>
                      {label(source.origin)} · {source.path}
                      <br />
                      <code>
                        {source.commit_sha?.slice(0, 7)} · lines{" "}
                        {source.location?.start_line ?? "?"}–
                        {source.location?.end_line ?? "?"}
                      </code>
                    </span>
                    <ReviewLink
                      href={
                        source.placement === "EXACT"
                          ? codeUrl(
                              fullName,
                              source.commit_sha,
                              source.location
                            )
                          : null
                      }
                    >
                      View source
                    </ReviewLink>
                  </figcaption>
                  <pre className="max-h-[32rem] overflow-auto">
                    {source.excerpt}
                  </pre>
                </figure>
              ))}
          </div>
        </details>
        <details>
          <summary className="review-subtitle">
            Revision source availability
          </summary>
          <ul className="divide-y">
            {result.observations.map((observation) => (
              <li
                key={observation.observation_id}
                className="flex flex-wrap justify-between gap-2 py-2 text-xs"
              >
                <span className="break-all">
                  {label(observation.revision)} · {observation.path}
                </span>
                <span>
                  {observation.status === "PRESENT"
                    ? "Source present"
                    : observation.status === "ABSENT"
                      ? "Source absent"
                      : "Source unavailable"}
                  {observation.complete_file ? " · Complete file observed" : ""}
                </span>
              </li>
            ))}
          </ul>
        </details>
        <TechnicalDetails
          title="Original source observations and revision metadata"
          value={{
            revision: result.revision,
            observations: result.observations,
          }}
        />
      </ReviewCard>
      <ReviewCard
        title="Original architectural evidence"
        icon={<BookOpen className="size-4" />}
      >
        <p className="review-subtitle">
          Exact original quotations, grouped only by source identity and
          citation span. Every finding retains its own original evidence
          association.
        </p>
        <div className="grid gap-4 xl:grid-cols-2">
          {groups
            .filter((group) => group.source.kind === "ADR")
            .map((group, index) => (
              <figure key={index} className="review-quote">
                <figcaption className="review-code-header">
                  <span>
                    {group.source.source_name ??
                      "Governing architectural document"}
                    <br />
                    Associated with {group.findingIds.length} finding(s)
                  </span>
                  <ReviewLink
                    href={citationUrl(fullName, group.source, group.citation)}
                  >
                    View source evidence
                  </ReviewLink>
                </figcaption>
                <pre>{group.citation.quote}</pre>
                {!citationUrl(fullName, group.source, group.citation) && (
                  <p className="review-subtitle px-4 pb-3">
                    No verified public source link
                  </p>
                )}
                <div className="px-3 pb-3">
                  <TechnicalDetails
                    title="Original citation spans and finding associations"
                    value={group}
                  />
                </div>
              </figure>
            ))}
        </div>
        {!groups.some((group) => group.source.kind === "ADR") && (
          <p className="review-subtitle">
            No original ADR quotations were retained for these findings.
          </p>
        )}
        <details>
          <summary className="review-subtitle">
            Original code quotations and source references
          </summary>
          <div className="mt-3 space-y-3">
            {groups
              .filter((group) => group.source.kind !== "ADR")
              .map((group, index) => (
                <figure key={index} className="review-quote">
                  <figcaption className="review-code-header">
                    <span>
                      {label(group.source.origin)} · {group.source.path} ·{" "}
                      {group.findingIds.length} finding association(s)
                    </span>
                    <ReviewLink
                      href={citationUrl(fullName, group.source, group.citation)}
                    >
                      View source evidence
                    </ReviewLink>
                  </figcaption>
                  <pre>{group.citation.quote}</pre>
                  <div className="px-3 pb-3">
                    <TechnicalDetails
                      title="Original source citation and associations"
                      value={group}
                    />
                  </div>
                </figure>
              ))}
          </div>
        </details>
      </ReviewCard>
      <ReviewCard
        title="Independent challenger verification"
        icon={<ShieldCheck className="size-4" />}
      >
        <Verification result={result} />
      </ReviewCard>
      {associations && (
        <TechnicalDetails
          title="Governing decision and requirement associations"
          value={associations}
        />
      )}
      <TechnicalDetails
        title="Canonical evaluation evidence (technical inspection)"
        value={result}
      />
    </div>
  );
}
export function ReviewCoverage({
  result,
  lifecycle,
  deferred,
}: {
  result: EvaluationResult;
  lifecycle: string | null;
  deferred?: Record<string, unknown>[];
}) {
  const descriptions: Record<string, string> = {
    ASSESSED: "Assessed",
    NOT_APPLICABLE: "Not applicable",
    EXEMPT: "Exempt",
    UNCERTAIN: "Uncertain",
    BUDGET_OMITTED: "Omitted",
    ERROR: "Execution error",
  };
  const reasons = [
    ...new Set(
      result.limitations.map((limitation) => limitationSummary(limitation.code))
    ),
  ];
  return (
    <div className="space-y-5">
      <ReviewCard title="Coverage and limitations">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[
            ["Assessed", result.coverage.assessed],
            ["Uncertain", result.coverage.uncertain],
            ["Omitted", result.coverage.omitted],
            ["Execution errors", result.coverage.errors],
          ].map(([title, count]) => (
            <div key={title} className="rounded-lg border p-4">
              <p className="review-subtitle">{title}</p>
              <p className="mt-2 text-2xl font-semibold">{count}</p>
            </div>
          ))}
        </div>
        <p className="font-medium">Coverage: {label(result.coverage.scope)}</p>
        <p className="review-subtitle">
          {result.coverage.scope !== "COMPLETE_FOR_RETAINED_SCOPE"
            ? "Confirmed conflicts can coexist with incomplete coverage. Some architectural questions remain unresolved; this is not a completely assessed PR."
            : "Coverage is complete only for the retained evaluation scope."}
        </p>
        {reasons.map((reason) => (
          <p key={reason} className="review-subtitle">
            • {reason}
          </p>
        ))}
      </ReviewCard>
      <ReviewCard title="Assessment units">
        <div className="divide-y">
          {result.units.map((unit, index) => (
            <div
              key={unit.unit_id}
              className="flex flex-wrap items-center justify-between gap-2 py-3"
            >
              <div>
                <p className="font-medium">Review unit {index + 1}</p>
                <p className="review-subtitle break-all">
                  {result.plan.find((item) => item.unit_id === unit.unit_id)
                    ?.path ?? "Source path unavailable"}
                </p>
              </div>
              <span
                className={`review-badge ${unit.status === "UNCERTAIN" || unit.status === "ERROR" || unit.status === "BUDGET_OMITTED" ? "review-caution" : ""}`}
              >
                {descriptions[unit.status]}
              </span>
            </div>
          ))}
        </div>
        <p className="review-subtitle">
          {findingGroups(result).review.length} review-required allegations and{" "}
          {result.rejected_findings.length} rejected proof records remain in the
          retained evaluation.
        </p>
        <TechnicalDetails
          title="Unit dispositions and discovery details"
          value={{ units: result.units, limitations: result.limitations }}
        />
      </ReviewCard>
      <ReviewCard title="Orchestration and execution">
        <div className="flex flex-wrap gap-3">
          <span className="review-badge">
            Job lifecycle: {label(lifecycle)}
          </span>
          <span className="review-badge">
            Evaluator execution: {label(result.execution)}
          </span>
        </div>
        {lifecycle === "DEFERRED" && (
          <p className="review-subtitle">
            Some evaluation work is deferred even though a persisted evaluation
            result exists. Job lifecycle is separate from the architectural
            verdict and coverage.
          </p>
        )}
        {deferred ? (
          <TechnicalDetails
            title="Deferred evaluation metadata (technical inspection)"
            value={deferred}
          />
        ) : (
          <p className="review-subtitle">
            Historical deferred metadata is not included in this read contract.
            Current-revision deferred records are not applied to a historical
            selection.
          </p>
        )}
      </ReviewCard>
    </div>
  );
}
