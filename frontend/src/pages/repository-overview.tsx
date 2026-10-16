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
import { OverviewAttention } from "@/components/repositories/overview-attention";

export function Reviews({
  items,
  repositoryId,
}: {
  items: RecentReview[];
  repositoryId: string;
}) {
  return (
    <div className="divide-y">
      {items.map((review) => (
        <article key={review.job_id} className="space-y-2 px-5 py-4">
          <Link
            className="font-medium text-primary underline dark:text-sidebar-primary"
            to={prPath(repositoryId, review.pr_number)}
          >
            PR #{review.pr_number} — {review.title || "Title unavailable"}
          </Link>
          <p
            className="text-sm text-muted-foreground"
            title="Counts represent governing-rule findings, not necessarily separate bugs. Rejected records are not confirmed violations."
          >
            <span
              className={
                review.outcome === "CONFLICTS_FOUND"
                  ? "text-destructive"
                  : review.outcome === "REVIEW_REQUIRED"
                    ? "text-amber-700 dark:text-amber-300"
                    : undefined
              }
            >
              {label(review.outcome)}
            </span>{" "}
            · {review.confirmed_conflicts} confirmed ·{" "}
            {review.review_required_findings} requiring review · Coverage:{" "}
            {label(review.coverage?.scope)}
          </p>
          <p className="text-xs text-muted-foreground">
            {dateLabel(review.evaluated_at)} ·{" "}
            <span
              title={`Compared with the last persisted GitHub base/head observation (${dateLabel(review.observed_at ?? null)}), not a live branch check.`}
            >
              {label(review.freshness)}
            </span>
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
      title: "Architectural Decisions",
      metric: kpis.architecture_decisions,
      fallback: "Count unavailable",
      description: "Recorded decisions",
      Icon: FileText,
    },
    {
      title: "Decisions at Risk",
      metric: kpis.decisions_at_risk,
      fallback: "Not evaluated yet",
      description: `${overview.risk_breakdown?.confirmed_decisions ?? 0} confirmed · ${overview.risk_breakdown?.potential_decisions ?? 0} potential`,
      Icon: ShieldAlert,
    },
    {
      title: "PRs Analyzed",
      metric: kpis.prs_analyzed,
      fallback: "Analysis not available yet",
      description: "Distinct evaluated pull requests",
      Icon: GitPullRequest,
    },
    {
      title: "Architectural Warnings",
      metric: kpis.architectural_warnings,
      fallback: "Analysis not available yet",
      description: `${overview.risk_breakdown?.confirmed_warnings ?? 0} supported governing-rule findings · ${overview.risk_breakdown?.potential_warnings ?? 0} unresolved or rejected records`,
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
                ? title === "Architectural Decisions"
                  ? "The graph query failed. A decision count could not be retrieved."
                  : "This metric could not be retrieved."
                : description}
            </p>
          </article>
        ))}
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
        <OverviewAttention
          repositoryId={repository.id}
          risks={overview.decisions_needing_attention.items ?? []}
          total={overview.decisions_needing_attention.total ?? 0}
          additionalTotal={overview.risk_breakdown?.unmapped_requirements ?? 0}
          available={
            overview.decisions_needing_attention.availability.status ===
            "available"
          }
        />
        <p className="border-t px-5 py-3 text-xs text-muted-foreground">
          {overview.risk_breakdown?.uncertain_units ?? 0} uncertain units. Risk
          associations are based on the latest persisted GitHub observation.
        </p>
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
