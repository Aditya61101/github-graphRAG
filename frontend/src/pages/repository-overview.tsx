import { Link } from "react-router";
import {
  FileText,
  GitPullRequest,
  ShieldAlert,
  TriangleAlert,
} from "lucide-react";
import { useRepositoryOverview } from "@/layouts/repository-layout";
import type { Availability, OverviewMetric } from "@/types/overview";
import type { RecentReview } from "@/types/reviews";
import { dateLabel, label, prPath } from "@/lib/review-presentation";
import { DecisionRisks } from "@/components/repositories/decision-risks";

function Reviews({
  items,
  repositoryId,
}: {
  items: RecentReview[];
  repositoryId: string;
}) {
  return (
    <div className="divide-y">
      {items.map((review) => (
        <article key={review.job_id} className="space-y-2 p-5">
          <Link
            className="font-medium text-primary underline dark:text-sidebar-primary"
            to={prPath(repositoryId, review.pr_number)}
          >
            PR #{review.pr_number}: {label(review.outcome)}
          </Link>
          <p className="text-sm">
            {review.confirmed_conflicts} supported governing-rule findings ·{" "}
            {review.review_required_findings} requiring review ·{" "}
            {review.rejected_findings} rejected records
          </p>
          <p className="text-xs text-muted-foreground">
            {dateLabel(review.evaluated_at)} · {label(review.freshness)} ·
            Coverage: {label(review.coverage?.scope)}
          </p>
        </article>
      ))}
    </div>
  );
}

function metricValue(metric: OverviewMetric, fallback: string) {
  if (metric.availability.status === "available") return metric.value;
  if (metric.availability.status === "error") return "Count unavailable";
  return fallback;
}

function sectionMessage(availability: Availability, unavailable: string) {
  return availability.status === "error"
    ? "Unable to load this section. Please try again later."
    : unavailable;
}

export default function RepositoryOverviewPage() {
  const overview = useRepositoryOverview();
  const { repository, kpis } = overview;
  const base = `/projects/${encodeURIComponent(repository.id)}`;
  const metrics = [
    {
      title: "Architecture Decisions",
      metric: kpis.architecture_decisions,
      fallback: "Count unavailable",
      description: "Distinct extracted / persisted decision nodes",
      Icon: FileText,
    },
    {
      title: "Decisions at Risk",
      metric: kpis.decisions_at_risk,
      fallback: "Not evaluated yet",
      description: `${overview.risk_breakdown?.confirmed_decisions ?? 0} confirmed · ${overview.risk_breakdown?.potential_decisions ?? 0} potential, from current observed open PRs. Unmapped requirements are counted separately.`,
      Icon: ShieldAlert,
    },
    {
      title: "PRs Analyzed",
      metric: kpis.prs_analyzed,
      fallback: "Analysis not available yet",
      description:
        "Distinct PRs with a persisted evaluation, across all revisions; not the number of runs",
      Icon: GitPullRequest,
    },
    {
      title: "Architectural Warnings",
      metric: kpis.architectural_warnings,
      fallback: "Analysis not available yet",
      description: `${overview.risk_breakdown?.confirmed_warnings ?? 0} supported governing-rule findings · ${overview.risk_breakdown?.potential_warnings ?? 0} unresolved or rejected records. These are not necessarily separate bugs.`,
      Icon: TriangleAlert,
    },
  ];
  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
          Architecture Overview
        </h1>
        <p className="text-sm text-muted-foreground">
          Architectural decisions and review status for this repository.
        </p>
      </section>
      <section
        aria-label="Architecture metrics"
        className="grid gap-4 md:grid-cols-2"
      >
        {metrics.map(({ title, metric, fallback, description, Icon }) => (
          <article key={title} className="rounded-xl border bg-card p-5">
            <div className="flex items-start justify-between gap-3">
              <h2 className="text-sm font-medium text-muted-foreground">
                {title}
              </h2>
              <Icon
                aria-hidden="true"
                className="size-5 text-muted-foreground"
              />
            </div>
            <p
              className="mt-3 text-2xl font-semibold"
              data-availability={metric.availability.status}
            >
              {metricValue(metric, fallback)}
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              {metric.availability.status === "error"
                ? title === "Architecture Decisions"
                  ? "The graph query failed. A decision count could not be retrieved."
                  : "This metric could not be retrieved."
                : description}
            </p>
          </article>
        ))}
      </section>
      <section
        className="rounded-xl border bg-card"
        aria-labelledby="review-activity-title"
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b p-5">
          <h2 id="review-activity-title" className="font-semibold">
            Architectural review activity
          </h2>
          <span className="text-xs text-muted-foreground">
            Last {overview.review_activity.window_days} days
          </span>
        </div>
        {overview.review_activity.availability.status === "available" &&
        overview.review_activity.items?.length ? (
          <Reviews
            items={overview.review_activity.items}
            repositoryId={repository.id}
          />
        ) : (
          <div className="p-6 text-muted-foreground">
            <p className="text-sm">
              {sectionMessage(
                overview.review_activity.availability,
                overview.review_activity.availability.status === "available"
                  ? "No architectural reviews in this period."
                  : "Architectural review activity is unavailable."
              )}
            </p>
          </div>
        )}
      </section>
      <section
        className="rounded-xl border bg-card"
        aria-labelledby="attention-title"
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b p-5">
          <h2 id="attention-title" className="font-semibold">
            Decisions needing attention
          </h2>
          <Link
            to={`${base}/decisions`}
            className="text-sm text-primary hover:underline dark:text-sidebar-primary"
          >
            View all decisions
          </Link>
        </div>
        <div className="p-5">
          <DecisionRisks
            repositoryId={repository.id}
            risks={overview.decisions_needing_attention.items ?? []}
            total={overview.decisions_needing_attention.total ?? 0}
            available={
              overview.decisions_needing_attention.availability.status ===
              "available"
            }
          />
          <p className="mt-3 text-xs text-muted-foreground">
            {overview.risk_breakdown?.unmapped_requirements ?? 0} requirements
            without an explicit decision mapping ·{" "}
            {overview.risk_breakdown?.uncertain_units ?? 0} uncertain units.
            Risk associations are based on the latest persisted GitHub
            observation.
          </p>
        </div>
        {overview.decisions_needing_attention.availability.status !==
          "available" && (
          <p className="p-5 text-sm text-muted-foreground">
            {sectionMessage(
              overview.decisions_needing_attention.availability,
              "Decision risk assessment is unavailable."
            )}
          </p>
        )}
      </section>
      <section
        className="rounded-xl border bg-card"
        aria-labelledby="recent-reviews-title"
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b p-5">
          <h2 id="recent-reviews-title" className="font-semibold">
            Recent architectural PR reviews
          </h2>
          <Link
            to={`${base}/pull-requests`}
            className="text-sm text-primary hover:underline dark:text-sidebar-primary"
          >
            View all PRs
          </Link>
        </div>
        {overview.recent_pr_reviews.availability.status === "available" &&
        overview.recent_pr_reviews.items?.length ? (
          <Reviews
            items={overview.recent_pr_reviews.items}
            repositoryId={repository.id}
          />
        ) : (
          <p className="p-5 text-sm text-muted-foreground">
            {sectionMessage(
              overview.recent_pr_reviews.availability,
              overview.recent_pr_reviews.availability.status === "available"
                ? "No architectural PR reviews yet."
                : "Architectural PR reviews are unavailable."
            )}
          </p>
        )}
      </section>
      <footer
        className="flex flex-wrap gap-x-6 gap-y-2 border-t pt-4 text-xs text-muted-foreground"
        aria-label="Repository indexing metadata"
      >
        <span>
          Tracked branch:{" "}
          <span className="font-medium text-foreground">
            {repository.tracked_branch}
          </span>
        </span>
        <span>
          Last successfully indexed commit:{" "}
          <code title={repository.indexed_commit_sha ?? undefined}>
            {repository.indexed_commit_sha?.slice(0, 7) ?? "Not indexed yet"}
          </code>
        </span>
        <span>Current RKG ingestion: {repository.status}</span>
      </footer>
    </div>
  );
}
