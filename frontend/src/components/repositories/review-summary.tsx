import type { ReactNode } from "react";
import axios from "axios";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import {
  checkUrl,
  dateLabel,
  githubRepo,
  label,
  prPath,
} from "@/lib/review-presentation";
import type {
  EvaluationCoverage,
  PullRequestResponse,
} from "@/types/evaluation.generated";

export function External({
  href,
  children,
}: {
  href?: string | null;
  children: ReactNode;
}) {
  return href ? (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-primary underline underline-offset-4 dark:text-sidebar-primary"
    >
      {children}
    </a>
  ) : null;
}
export function ReviewFailure({
  error,
  retry,
}: {
  error: unknown;
  retry: () => void;
}) {
  const status = axios.isAxiosError(error) ? error.response?.status : null;
  const text =
    status === 401
      ? "Your session or GitHub access needs to be renewed."
      : status === 403
        ? "You do not have access to this repository's reviews."
        : status === 404
          ? "This pull request or evaluation could not be found."
          : "Unable to load architectural reviews. Please try again.";
  return (
    <div role="alert" className="space-y-3 rounded-xl border p-5">
      <p>{text}</p>
      <Button variant="outline" onClick={retry}>
        Try again
      </Button>
    </div>
  );
}
export function Coverage({
  coverage,
}: {
  coverage: EvaluationCoverage | null;
}) {
  if (!coverage) return <p>Coverage not available.</p>;
  return (
    <div className="space-y-2 text-sm">
      <p>
        <strong>Coverage: {label(coverage.scope)}</strong>
      </p>
      <p>
        {coverage.assessed} assessed · {coverage.uncertain} uncertain ·{" "}
        {coverage.omitted} omitted · {coverage.errors} execution errors
      </p>
      {(coverage.scope !== "COMPLETE_FOR_RETAINED_SCOPE" ||
        coverage.uncertain + coverage.omitted + coverage.errors > 0) && (
        <p className="text-muted-foreground">
          Some architectural questions remain unresolved. This evaluation does
          not establish that the PR is free of conflicts.
        </p>
      )}
    </div>
  );
}
export function PRSummary({ pr }: { pr: PullRequestResponse }) {
  const evaluation = pr.evaluation;
  const repo = githubRepo(pr.repository_full_name);
  return (
    <article className="space-y-3 rounded-xl border bg-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link
            className="font-semibold text-primary hover:underline dark:text-sidebar-primary"
            to={prPath(pr.repository_id, pr.pr_number)}
          >
            #{pr.pr_number} {pr.title ?? "Untitled pull request"}
          </Link>
          <p className="mt-1 text-sm text-muted-foreground">
            {pr.author ?? "Author unavailable"} · {pr.state}
          </p>
        </div>
        <External href={repo ? `${repo}/pull/${pr.pr_number}` : null}>
          GitHub PR
        </External>
      </div>
      <p className="font-medium">
        {evaluation
          ? label(evaluation.outcome)
          : "No evaluation for the observed revision"}
      </p>
      <p className="text-sm">
        {label(pr.freshness)} · Metadata observed: {dateLabel(pr.observed_at)}
      </p>
      <p className="text-xs text-muted-foreground">
        Freshness compares against the last persisted GitHub observation; it is
        not a live branch check.
      </p>
      {evaluation ? (
        <>
          <p className="text-sm">
            {evaluation.confirmed_conflicts} supported governing-rule findings ·{" "}
            {evaluation.review_required_findings} requiring review ·{" "}
            {evaluation.rejected_findings} rejected records
          </p>
          <Coverage coverage={evaluation.coverage} />
          <p className="text-sm">
            Last evaluation: {dateLabel(evaluation.evaluated_at)} · Check:{" "}
            {label(evaluation.publication.status)}
          </p>
          <External
            href={checkUrl(
              pr.repository_full_name,
              evaluation.publication.check_run_id
            )}
          >
            View GitHub Check
          </External>
        </>
      ) : (
        <p className="text-sm text-muted-foreground">
          Previous revision evidence, if any, is available in evaluation
          history.
        </p>
      )}
    </article>
  );
}
export function Pagination({
  offset,
  limit,
  count,
  total,
  onPage,
}: {
  offset: number;
  limit: number;
  count: number;
  total: number;
  onPage: (value: number) => void;
}) {
  return (
    <nav
      aria-label="Pagination"
      className="flex flex-wrap items-center justify-between gap-3"
    >
      <Button
        variant="outline"
        disabled={offset === 0}
        onClick={() => onPage(Math.max(0, offset - limit))}
      >
        Previous
      </Button>
      <p role="status" className="text-sm">
        {total === 0
          ? "No entries"
          : `${count ? offset + 1 : 0}–${offset + count} of ${total}`}
      </p>
      <Button
        variant="outline"
        disabled={offset + limit >= total}
        onClick={() => onPage(offset + limit)}
      >
        Next
      </Button>
    </nav>
  );
}
