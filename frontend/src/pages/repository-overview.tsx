import { Link } from "react-router";
import {
  Activity,
  FileText,
  GitPullRequest,
  ShieldAlert,
  TriangleAlert,
} from "lucide-react";
import { useRepositoryOverview } from "@/layouts/repository-layout";
import type { Availability, OverviewMetric } from "@/types/overview";

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
      description: "Decision risk assessment is not available yet",
      Icon: ShieldAlert,
    },
    {
      title: "PRs Analyzed",
      metric: kpis.prs_analyzed,
      fallback: "Analysis not available yet",
      description: "Completed architectural PR evaluations",
      Icon: GitPullRequest,
    },
    {
      title: "Architectural Warnings",
      metric: kpis.architectural_warnings,
      fallback: "Analysis not available yet",
      description: "Findings from architectural evaluations",
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
                ? "The graph query failed. A decision count could not be retrieved."
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
        <div className="flex min-h-52 flex-col items-center justify-center gap-3 p-6 text-center text-muted-foreground">
          <Activity aria-hidden="true" className="size-7" />
          <p className="text-sm">
            {sectionMessage(
              overview.review_activity.availability,
              "Architectural review activity will appear once PR analysis is enabled."
            )}
          </p>
        </div>
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
            className="text-sm text-primary hover:underline"
          >
            View all decisions
          </Link>
        </div>
        <p className="p-5 text-sm text-muted-foreground">
          {sectionMessage(
            overview.decisions_needing_attention.availability,
            "Not evaluated yet. Decisions needing attention will appear once risk assessment is available."
          )}
        </p>
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
            className="text-sm text-primary hover:underline"
          >
            View all PRs
          </Link>
        </div>
        <p className="p-5 text-sm text-muted-foreground">
          {sectionMessage(
            overview.recent_pr_reviews.availability,
            "Analysis not available yet. Completed architectural reviews will appear once PR analysis is enabled."
          )}
        </p>
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
