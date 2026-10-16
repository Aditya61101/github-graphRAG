import { Link } from "react-router";
import { FileCheck2, GitPullRequest, type LucideIcon } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { checkUrl, dateLabel, label, prPath } from "@/lib/review-presentation";
import type { PullRequestResponse } from "@/types/evaluation.generated";
import "./pull-request-table.css";

function IconLink({
  href,
  name,
  icon: Icon,
}: {
  href: string;
  name: string;
  icon: LucideIcon;
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={name}
          />
        }
      >
        <Icon size={17} aria-hidden="true" />
      </TooltipTrigger>
      <TooltipContent>{name}</TooltipContent>
    </Tooltip>
  );
}

function reviewStatus(pr: PullRequestResponse) {
  const evaluation = pr.evaluation;
  if (pr.freshness === "STALE")
    return { text: "Stale analysis", tone: "caution" };
  if (!evaluation) return { text: "Not evaluated", tone: "muted" };
  if (
    evaluation.confirmed_conflicts > 0 ||
    evaluation.outcome === "CONFLICTS_FOUND"
  )
    return { text: "Confirmed conflicts", tone: "danger" };
  if (
    evaluation.review_required_findings > 0 ||
    evaluation.outcome === "REVIEW_REQUIRED"
  )
    return { text: "Requires review", tone: "caution" };
  if (evaluation.outcome === "NO_CONFLICTS_FOUND") {
    const complete =
      evaluation.coverage?.scope === "COMPLETE_FOR_RETAINED_SCOPE" &&
      evaluation.execution === "COMPLETED";
    return {
      text: complete
        ? "No confirmed conflicts"
        : "No confirmed conflicts · incomplete review",
      tone: complete ? "neutral" : "caution",
    };
  }
  if (["RUNNING", "QUEUED"].includes(evaluation.status))
    return {
      text: evaluation.status === "QUEUED" ? "Evaluation queued" : "Evaluating",
      tone: "muted",
    };
  return {
    text:
      evaluation.status === "FAILED"
        ? "Evaluation unavailable"
        : "Not evaluated",
    tone: "muted",
  };
}

export function PullRequestTable({ items }: { items: PullRequestResponse[] }) {
  return (
    <div className="pr-queue">
      <table role="table" aria-label="Pull request architectural reviews">
        <thead>
          <tr role="row">
            {[
              "Pull Request",
              "State",
              "Architectural Review",
              "Findings",
              "Coverage",
              "Last Evaluated",
              "Links",
            ].map((heading) => (
              <th role="columnheader" scope="col" key={heading}>
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((pr) => {
            const evaluation = pr.evaluation;
            const status = reviewStatus(pr);
            const coverage = evaluation?.coverage?.scope;
            const publishedCheck =
              evaluation?.publication.status === "PUBLISHED"
                ? checkUrl(
                    pr.repository_full_name,
                    evaluation.publication.check_run_id
                  )
                : null;
            return (
              <tr role="row" key={pr.pr_number}>
                <td role="cell" className="queue-identity">
                  <Link
                    to={prPath(pr.repository_id, pr.pr_number)}
                    className="queue-title"
                  >
                    #{pr.pr_number} {pr.title || "Title unavailable"}
                  </Link>
                  <span className="queue-secondary">
                    {pr.author ? `by ${pr.author}` : "Author unavailable"}
                  </span>
                </td>
                <td role="cell" data-label="State" className="queue-state">
                  <span className="queue-state-label">
                    {
                      {
                        open: "Open",
                        closed: "Closed",
                        merged: "Merged",
                        unknown: "Unknown",
                      }[pr.state]
                    }
                  </span>
                </td>
                <td
                  role="cell"
                  data-label="Architectural review"
                  className="queue-review"
                >
                  <span className={`queue-verdict queue-${status.tone}`}>
                    {status.text}
                  </span>
                  <span
                    className="queue-secondary"
                    title={`Last GitHub metadata observation: ${dateLabel(pr.observed_at)}. Compared with the persisted base/head, not a live branch check.`}
                  >
                    {label(pr.freshness)}
                  </span>
                </td>
                <td
                  role="cell"
                  data-label="Findings"
                  className="queue-findings"
                  title="Supported governing-rule findings; these are not necessarily separate bugs."
                >
                  {evaluation ? (
                    <>
                      <span
                        className={
                          evaluation.confirmed_conflicts ? "queue-danger" : ""
                        }
                      >
                        {evaluation.confirmed_conflicts} confirmed
                      </span>
                      <span
                        className={
                          evaluation.review_required_findings
                            ? "queue-caution"
                            : "queue-secondary"
                        }
                      >
                        {evaluation.review_required_findings} requiring review
                      </span>
                    </>
                  ) : (
                    <span className="queue-secondary">Not available</span>
                  )}
                </td>
                <td
                  role="cell"
                  data-label="Coverage"
                  className="queue-coverage"
                >
                  <span
                    className={
                      coverage && coverage !== "COMPLETE_FOR_RETAINED_SCOPE"
                        ? "queue-caution"
                        : ""
                    }
                    title={
                      coverage === "COMPLETE_FOR_RETAINED_SCOPE"
                        ? "Complete for retained scope only"
                        : undefined
                    }
                  >
                    {coverage === "COMPLETE_FOR_RETAINED_SCOPE"
                      ? "Complete"
                      : coverage
                        ? label(coverage)
                        : "Not available"}
                  </span>
                </td>
                <td
                  role="cell"
                  data-label="Last evaluated"
                  className="queue-date"
                >
                  {evaluation?.evaluated_at != null ? (
                    <time
                      dateTime={new Date(
                        evaluation.evaluated_at * 1000
                      ).toISOString()}
                    >
                      {dateLabel(evaluation.evaluated_at)}
                    </time>
                  ) : (
                    <span className="queue-secondary">Not available</span>
                  )}
                </td>
                <td role="cell" data-label="Links" className="queue-links">
                  <div className="queue-link-actions">
                    <IconLink
                      href={pr.github_url}
                      name="GitHub PR"
                      icon={GitPullRequest}
                    />
                    {publishedCheck && (
                      <IconLink
                        href={publishedCheck}
                        name="GitHub Check"
                        icon={FileCheck2}
                      />
                    )}
                  </div>
                  {!publishedCheck && (
                    <span className="queue-secondary">
                      {evaluation
                        ? `Check: ${label(evaluation.publication.status)}`
                        : "Check unavailable"}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
