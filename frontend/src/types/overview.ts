export type Availability =
  | { status: "available"; reason: null }
  | { status: "unavailable"; reason: "engine_not_implemented" }
  | { status: "error"; reason: "graph_query_failed" };

export type OverviewMetric = {
  availability: Availability;
  value: number | null;
};

export type RepositoryOverview = {
  repository: {
    id: string;
    owner: string;
    name: string;
    full_name: string;
    repository_url: string;
    tracked_branch: string;
    indexed_commit_sha: string | null;
    status: string;
  };
  permissions: { can_view_settings: boolean };
  decision_count_basis: "distinct_persisted_nodes";
  kpis: {
    architecture_decisions: OverviewMetric;
    decisions_at_risk: OverviewMetric;
    prs_analyzed: OverviewMetric;
    architectural_warnings: OverviewMetric;
  };
  review_activity: { availability: Availability; window_days: 7 };
  decisions_needing_attention: { availability: Availability };
  recent_pr_reviews: { availability: Availability };
};
