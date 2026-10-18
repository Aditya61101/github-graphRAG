import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ChevronRight,
  ExternalLink,
  GitPullRequest,
} from "lucide-react";
import { useAuth } from "@/contexts/auth-context";
import { useRepositoryOverview } from "@/layouts/repository-layout";
import { pullRequestService } from "@/api/pull-request-service";
import { TechnicalDetails } from "@/components/repositories/technical-details";
import {
  Pagination,
  ReviewFailure,
} from "@/components/repositories/review-summary";
import {
  ReviewCard,
  ReviewCoverage,
  ReviewEvidence,
  ReviewFindings,
  ReviewLink,
  ReviewOverview,
} from "@/components/repositories/pr-review-evidence";
import type { ReviewTab } from "@/components/repositories/pr-review-evidence";
import { Button } from "@/components/ui/button";
import {
  ContentLoading,
  RefreshButton,
} from "@/components/core/query-feedback";
import { isAuthoritativeAccessError } from "@/lib/api-error";
import {
  checkUrl,
  dateLabel,
  findingGroups,
  githubRepo,
  label,
} from "@/lib/review-presentation";
import { readable } from "@/lib/pr-review-model";
import type {
  EvaluationResult,
  EvaluationSummary,
  PullRequestDetail as Detail,
  PullRequestResponse,
} from "@/types/evaluation.generated";
import "@/components/repositories/pr-review.css";

const tabs: ReviewTab[] = [
  "Overview",
  "Findings",
  "Evidence",
  "Coverage",
  "History",
];
export default function RepositoryPullRequestDetailPage() {
  const { repository } = useRepositoryOverview();
  const { prNumber = "" } = useParams();
  if (!/^[1-9]\d*$/.test(prNumber) || !Number.isSafeInteger(Number(prNumber)))
    return <p role="alert">Invalid pull request number.</p>;
  return (
    <PullRequestDetail
      key={`${repository.id}:${prNumber}`}
      id={repository.id}
      number={Number(prNumber)}
    />
  );
}
function PullRequestDetail({ id, number }: { id: string; number: number }) {
  const { user, token } = useAuth();
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<EvaluationSummary | null>(null);
  const scope = [user?.id, token, id, number];
  const query = useQuery({
    queryKey: ["pull-request-detail", ...scope, offset],
    queryFn: ({ signal }) =>
      pullRequestService.detail(id, number, offset, signal),
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const historical = useQuery({
    queryKey: ["evaluation-result", ...scope, selected?.job_id],
    queryFn: ({ signal }) =>
      pullRequestService.result(id, selected!.job_id, signal),
    enabled: selected !== null,
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  if (query.isPending)
    return <ContentLoading label="Loading pull request analysis..." cards />;
  if (
    (query.isError &&
      (!query.data || isAuthoritativeAccessError(query.error))) ||
    isAuthoritativeAccessError(historical.error)
  )
    return (
      <ReviewFailure
        error={query.error ?? historical.error}
        retry={() =>
          void (
            isAuthoritativeAccessError(historical.error) ? historical : query
          ).refetch({ cancelRefetch: false })
        }
      />
    );
  const result = selected ? historical.data : query.data!.result;
  const identityMatches =
    !result ||
    (result.revision.repository_id === id &&
      result.revision.pull_request_number === number &&
      (!selected ||
        (result.revision.base_sha === selected.base_sha &&
          result.revision.head_sha === selected.head_sha &&
          result.revision.revision_key === selected.revision_key)));
  return (
    <>
      <RefreshButton
        busy={query.isFetching}
        label="Refresh review"
        onRefresh={() => {
          void query.refetch({ cancelRefetch: false });
        }}
      />
      {query.isError && (
        <p role="alert" className="text-sm text-muted-foreground">
          Unable to refresh this review. Previously loaded review is shown. Try
          Refresh review again.
        </p>
      )}
      <PRReview
        detail={query.data!}
        result={identityMatches ? (result ?? null) : null}
        selected={selected}
        select={setSelected}
        offset={offset}
        onPage={setOffset}
        pending={selected !== null && historical.isPending}
        error={
          !identityMatches
            ? new Error("Evaluation revision mismatch")
            : selected
              ? historical.error
              : null
        }
        retry={() => void historical.refetch({ cancelRefetch: false })}
      />
    </>
  );
}
export function VerdictPanel({
  result,
  evaluation,
  selected,
}: {
  result: EvaluationResult | null;
  evaluation: EvaluationSummary | null;
  selected: boolean;
}) {
  const groups = result && findingGroups(result);
  const danger = result?.outcome === "CONFLICTS_FOUND";
  const primary = groups?.confirmed[0] ?? groups?.review[0];
  return (
    <section
      aria-label="Architectural verdict"
      className={`review-verdict ${danger ? "review-danger" : "review-caution"}`}
    >
      <AlertCircle
        aria-hidden="true"
        className={`mt-0.5 size-6 shrink-0 ${danger ? "text-destructive" : "text-[var(--review-amber)]"}`}
      />
      <div className="min-w-0 flex-1 space-y-2">
        <h2 className="text-lg font-semibold tracking-tight">
          {selected ? "Selected evaluation: " : ""}
          {result
            ? label(result.outcome)
            : "No evaluation for the observed revision"}
        </h2>
        <p className="max-w-5xl text-sm leading-relaxed text-muted-foreground">
          {primary
            ? readable(primary.claim, 225)
            : result
              ? "Interpret this verdict together with retained coverage and unresolved evidence."
              : "No canonical result is available for the observed revision. Inspect a completed historical evaluation in History."}
        </p>
        {groups && (
          <p className="text-xs text-muted-foreground">
            Supported governing-rule findings are not necessarily independent
            bugs.
          </p>
        )}
      </div>
      {result && (
        <div className="flex max-w-sm flex-wrap gap-2">
          <span className={`review-badge ${danger ? "review-danger" : ""}`}>
            {groups!.confirmed.length} supported governing-rule findings
          </span>
          <span
            className={`review-badge ${groups!.review.length ? "review-caution" : ""}`}
          >
            {groups!.review.length} requires review
          </span>
          <span
            className={`review-badge ${result.coverage.scope !== "COMPLETE_FOR_RETAINED_SCOPE" ? "review-caution" : ""}`}
          >
            {label(result.coverage.scope)} coverage
          </span>
          <span
            className={`review-badge ${result.coverage.uncertain ? "review-caution" : ""}`}
          >
            {result.coverage.uncertain} uncertain units
          </span>
          <span className="review-badge">
            GitHub Check: {label(evaluation?.publication.status)}
          </span>
        </div>
      )}
    </section>
  );
}
function History({
  detail,
  selected,
  offset,
  onPage,
  select,
}: {
  detail: Detail;
  selected: EvaluationSummary | null;
  offset: number;
  onPage: (offset: number) => void;
  select: (run: EvaluationSummary | null) => void;
}) {
  const pr = detail.pull_request;
  return (
    <ReviewCard title="Evaluation history">
      <p className="review-subtitle">
        Each evaluation preserves its own revision and evidence. Selecting a
        record only reads its stored result.
      </p>
      <Button variant="outline" onClick={() => select(null)}>
        Latest observed evaluation
      </Button>
      <div className="space-y-3">
        {detail.history.map((run) => (
          <article key={run.job_id} className="space-y-3 rounded-lg border p-4">
            <div className="flex flex-wrap justify-between gap-3">
              <p className="font-medium">
                {dateLabel(run.evaluated_at ?? run.created_at)}
              </p>
              <span className="review-badge">
                {pr.metadata_freshness === "OBSERVED" &&
                pr.head_sha === run.head_sha &&
                pr.base_sha === run.base_sha
                  ? "Matches observed revision"
                  : "Historical / freshness unverified"}
              </span>
            </div>
            <p>
              {label(run.outcome)} · Coverage: {label(run.coverage?.scope)}
            </p>
            <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
              <span>Job lifecycle: {label(run.status)}</span>
              <span>Evaluator execution: {label(run.execution)}</span>
              <span>
                Base{" "}
                <code title={run.base_sha}>{run.base_sha.slice(0, 7)}</code> →
                Head{" "}
                <code title={run.head_sha}>{run.head_sha.slice(0, 7)}</code>
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-4">
              <Button
                variant="outline"
                size="sm"
                disabled={run.evaluated_at === null}
                aria-pressed={selected?.job_id === run.job_id}
                onClick={() => select(run)}
              >
                Inspect evaluation evidence
              </Button>
              <ReviewLink
                href={checkUrl(
                  pr.repository_full_name,
                  run.publication.check_run_id
                )}
              >
                View this evaluation's GitHub Check ↗
              </ReviewLink>
            </div>
            <TechnicalDetails
              title="Evaluation revision and publication metadata"
              value={run}
            />
          </article>
        ))}
      </div>
      {!detail.history.length && (
        <p className="review-subtitle">No evaluation history on this page.</p>
      )}
      <Pagination
        offset={offset}
        limit={detail.limit}
        count={detail.history.length}
        total={detail.history_total}
        onPage={onPage}
      />
    </ReviewCard>
  );
}
function Header({
  pr,
  evaluation,
}: {
  pr: PullRequestResponse;
  evaluation: EvaluationSummary | null;
}) {
  const repo = githubRepo(pr.repository_full_name);
  const check = checkUrl(
    pr.repository_full_name,
    evaluation?.publication.check_run_id
  );
  return (
    <header className="space-y-4">
      <nav
        aria-label="Pull request breadcrumb"
        className="flex items-center gap-2 text-xs text-muted-foreground"
      >
        <Link
          className="review-link"
          to={`/projects/${encodeURIComponent(pr.repository_id)}/pull-requests`}
        >
          Pull Requests
        </Link>
        <ChevronRight aria-hidden="true" className="size-3" />
        <span>#{pr.pr_number}</span>
      </nav>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <h1 className="min-w-0 flex-1 text-xl font-semibold tracking-tight break-words sm:text-2xl">
          PR #{pr.pr_number}: {pr.title ?? "Untitled pull request"}
        </h1>
        <div className="flex flex-wrap gap-2">
          {repo && (
            <Button
              variant="outline"
              nativeButton={false}
              render={
                <a
                  href={`${repo}/pull/${pr.pr_number}`}
                  target="_blank"
                  rel="noopener noreferrer"
                />
              }
            >
              <GitPullRequest className="size-4" />
              View on GitHub
              <ExternalLink className="size-3" />
            </Button>
          )}
          {check && (
            <Button
              variant="outline"
              nativeButton={false}
              render={
                <a href={check} target="_blank" rel="noopener noreferrer" />
              }
            >
              <ExternalLink className="size-3" />
              View GitHub Check
            </Button>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
        <span>{pr.author ?? "Author unavailable"}</span>
        <span>PR state: {pr.state}</span>
        <span className="review-badge">{label(pr.freshness)}</span>
        <details>
          <summary>Revision and observation details</summary>
          <div className="space-y-2 py-2">
            <p>Metadata observed: {dateLabel(pr.observed_at)}</p>
            <p>Last evaluation: {dateLabel(pr.evaluation?.evaluated_at)}</p>
            <p className="break-all">
              Base <code>{pr.base_sha ?? "Unknown"}</code> → Head{" "}
              <code>{pr.head_sha ?? "Unknown"}</code>
            </p>
            <p>
              Freshness compares the stored base/head with the last persisted
              GitHub observation, not a live branch check.
            </p>
          </div>
        </details>
      </div>
    </header>
  );
}
export function PRReview({
  detail,
  result,
  selected,
  select,
  offset,
  onPage,
  pending = false,
  error = null,
  retry = () => {},
}: {
  detail: Detail;
  result: EvaluationResult | null;
  selected: EvaluationSummary | null;
  select: (run: EvaluationSummary | null) => void;
  offset: number;
  onPage: (offset: number) => void;
  pending?: boolean;
  error?: unknown;
  retry?: () => void;
}) {
  const [search, setSearch] = useSearchParams();
  const requestedTab = search.get("tab") as ReviewTab | null;
  const tab =
    requestedTab && tabs.includes(requestedTab) ? requestedTab : "Overview";
  const setTab = (next: ReviewTab) =>
    setSearch(
      (previous) => {
        const params = new URLSearchParams(previous);
        if (next === "Overview") params.delete("tab");
        else params.set("tab", next);
        return params;
      },
      { replace: true }
    );
  const [focusFinding, setFocusFinding] = useState<string>();
  const pr = detail.pull_request;
  const evaluation = selected ?? pr.evaluation;
  const matchesObserved = Boolean(
    result &&
    pr.metadata_freshness === "OBSERVED" &&
    pr.base_sha === result.revision.base_sha &&
    pr.head_sha === result.revision.head_sha
  );
  useEffect(() => {
    if (tab !== "Findings" || !focusFinding) return;
    const element = document.getElementById(`finding-${focusFinding}`);
    element?.focus({ preventScroll: true });
    element?.scrollIntoView({ block: "start", behavior: "instant" });
  }, [tab, focusFinding]);
  const open = (next: ReviewTab, finding?: string) => {
    setFocusFinding(finding);
    setTab(next);
  };
  const selectRun = (run: EvaluationSummary | null) => {
    select(run);
    setTab("Overview");
    setFocusFinding(undefined);
  };
  return (
    <div className="pr-review space-y-6" data-testid="pr-review">
      <Header pr={pr} evaluation={evaluation} />
      {selected && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3">
          <div>
            <p className="font-medium">Selected historical evaluation</p>
            <p className="review-subtitle">
              {dateLabel(selected.evaluated_at)} ·{" "}
              {matchesObserved
                ? "Matches observed revision"
                : "Historical or stale evidence — does not establish the current PR verdict"}
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => selectRun(null)}>
            Return to observed evaluation
          </Button>
        </div>
      )}
      {!pending && !error && (
        <VerdictPanel
          result={result}
          evaluation={evaluation}
          selected={selected !== null}
        />
      )}
      <div
        role="tablist"
        aria-label="PR review sections"
        className="review-tabs"
        onKeyDown={(event) => {
          const index = tabs.indexOf(tab);
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
            return;
          event.preventDefault();
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? tabs.length - 1
                : (index +
                    (event.key === "ArrowRight" ? 1 : -1) +
                    tabs.length) %
                  tabs.length;
          setTab(tabs[next]);
          setFocusFinding(undefined);
          event.currentTarget
            .querySelectorAll<HTMLButtonElement>('[role="tab"]')
            .item(next)
            ?.focus();
        }}
      >
        {tabs.map((name) => (
          <button
            key={name}
            id={`review-tab-${name}`}
            role="tab"
            aria-selected={tab === name}
            aria-controls={`review-panel-${name}`}
            tabIndex={tab === name ? 0 : -1}
            onClick={() => open(name)}
          >
            {name}
          </button>
        ))}
      </div>
      <section
        id={`review-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`review-tab-${tab}`}
        tabIndex={0}
        className="min-w-0 outline-none"
        aria-busy={pending}
      >
        {tab === "History" ? (
          <History
            detail={detail}
            selected={selected}
            select={selectRun}
            offset={offset}
            onPage={onPage}
          />
        ) : pending ? (
          <ContentLoading label="Loading historical evidence..." cards />
        ) : error ? (
          <ReviewFailure error={error} retry={retry} />
        ) : !result ? (
          <div className="review-card review-subtitle">
            No canonical result is available for the observed revision. Inspect
            a completed historical evaluation in History.
          </div>
        ) : tab === "Overview" ? (
          <ReviewOverview
            result={result}
            fullName={pr.repository_full_name}
            open={open}
          />
        ) : tab === "Findings" ? (
          <ReviewFindings
            result={result}
            fullName={pr.repository_full_name}
            onEvidence={() => open("Evidence")}
          />
        ) : tab === "Evidence" ? (
          <ReviewEvidence
            result={result}
            fullName={pr.repository_full_name}
            associations={selected ? undefined : detail.governing_rules}
          />
        ) : (
          <ReviewCoverage
            result={result}
            lifecycle={evaluation?.status ?? null}
            deferred={selected ? undefined : detail.deferred}
          />
        )}
      </section>
      {tabs
        .filter((name) => name !== tab)
        .map((name) => (
          <section
            key={name}
            id={`review-panel-${name}`}
            role="tabpanel"
            aria-labelledby={`review-tab-${name}`}
            hidden
          />
        ))}
    </div>
  );
}
