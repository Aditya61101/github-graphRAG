import { useState } from "react";
import { Link, useParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/contexts/auth-context";
import { useRepositoryOverview } from "@/layouts/repository-layout";
import { pullRequestService } from "@/api/pull-request-service";
import { EvaluationEvidence } from "@/components/repositories/evaluation-evidence";
import { TechnicalDetails } from "@/components/repositories/technical-details";
import {
  Coverage,
  External,
  Pagination,
  PRSummary,
  ReviewFailure,
} from "@/components/repositories/review-summary";
import { Button } from "@/components/ui/button";
import { checkUrl, dateLabel, label } from "@/lib/review-presentation";
import type { EvaluationSummary } from "@/types/evaluation.generated";

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
    return (
      <p role="status" aria-busy="true">
        Loading pull request analysis...
      </p>
    );
  if (query.isError)
    return (
      <ReviewFailure error={query.error} retry={() => void query.refetch()} />
    );
  const {
    pull_request: pr,
    result: currentResult,
    history,
    history_total,
  } = query.data;
  const result = selected ? historical.data : currentResult;
  const identityMatches =
    !result ||
    (result.revision.repository_id === id &&
      result.revision.pull_request_number === number &&
      (!selected ||
        (result.revision.base_sha === selected.base_sha &&
          result.revision.head_sha === selected.head_sha &&
          result.revision.revision_key === selected.revision_key)));
  const matchesObserved = Boolean(
    result &&
    pr.metadata_freshness === "OBSERVED" &&
    pr.base_sha === result.revision.base_sha &&
    pr.head_sha === result.revision.head_sha
  );
  return (
    <div className="space-y-6">
      <Link
        className="text-sm text-primary underline dark:text-sidebar-primary"
        to={`/projects/${encodeURIComponent(id)}/pull-requests`}
      >
        Back to pull requests
      </Link>
      <h1 className="text-2xl font-semibold">
        PR #{number} architectural review
      </h1>
      <PRSummary pr={pr} />
      <section className="space-y-3 rounded-xl border p-5">
        <h2 className="text-xl font-semibold">Evaluation history</h2>
        <p className="text-sm text-muted-foreground">
          Inspect preserved evidence for previous evaluations. Matching refers
          to both base and head of the last observed PR revision.
        </p>
        <Button
          variant={selected ? "outline" : "secondary"}
          onClick={() => setSelected(null)}
        >
          Latest observed evaluation
        </Button>
        <div className="space-y-3">
          {history.map((run) => (
            <article
              key={run.job_id}
              className="space-y-2 rounded-lg border p-3"
            >
              <p>
                {dateLabel(run.evaluated_at ?? run.created_at)} ·{" "}
                {label(run.status)} · {label(run.outcome)}
              </p>
              <p className="text-sm">
                {pr.metadata_freshness === "OBSERVED" &&
                pr.head_sha === run.head_sha &&
                pr.base_sha === run.base_sha
                  ? "Matches observed revision"
                  : "Historical / freshness unverified"}{" "}
                · Execution: {label(run.execution)}
              </p>
              <p className="text-xs text-muted-foreground">
                Base{" "}
                <code title={run.base_sha}>{run.base_sha.slice(0, 7)}</code> →
                Head{" "}
                <code title={run.head_sha}>{run.head_sha.slice(0, 7)}</code>
              </p>
              <Coverage coverage={run.coverage} />
              <Button
                size="sm"
                variant="outline"
                disabled={run.evaluated_at === null}
                aria-pressed={selected?.job_id === run.job_id}
                onClick={() => setSelected(run)}
              >
                Inspect evaluation evidence
              </Button>
            </article>
          ))}
        </div>
        {!history.length && <p>No evaluation history on this page.</p>}
        <Pagination
          offset={offset}
          limit={query.data.limit}
          count={history.length}
          total={history_total}
          onPage={setOffset}
        />
      </section>
      <section className="space-y-3" aria-live="polite">
        <h2 className="text-xl font-semibold">
          {selected
            ? "Selected historical evaluation"
            : "Latest evaluation evidence"}
        </h2>
        {selected && (
          <p className="text-sm">
            Recorded {dateLabel(selected.evaluated_at)} · Check:{" "}
            {label(selected.publication.status)}{" "}
            <External
              href={checkUrl(
                pr.repository_full_name,
                selected.publication.check_run_id
              )}
            >
              View this evaluation's GitHub Check
            </External>
          </p>
        )}
        {selected && historical.isPending ? (
          <p role="status" aria-busy="true">
            Loading historical evidence...
          </p>
        ) : selected && historical.isError ? (
          <ReviewFailure
            error={historical.error}
            retry={() => void historical.refetch()}
          />
        ) : !identityMatches ? (
          <p role="alert">
            Evaluation revision mismatch. Evidence cannot be displayed safely.
          </p>
        ) : result ? (
          <>
            <p className="font-semibold">{label(result.outcome)}</p>
            <p className="text-sm">
              {matchesObserved
                ? "Matches observed revision"
                : "Historical or stale evidence — does not establish the current PR verdict"}{" "}
              · Execution: {label(result.execution)}
            </p>
            <p className="text-xs text-muted-foreground">
              Base{" "}
              <code title={result.revision.base_sha}>
                {result.revision.base_sha.slice(0, 7)}
              </code>{" "}
              → Head{" "}
              <code title={result.revision.head_sha}>
                {result.revision.head_sha.slice(0, 7)}
              </code>
            </p>
            {!selected && (
              <TechnicalDetails
                title="Governing decision and requirement associations"
                value={query.data.governing_rules}
              />
            )}
            <EvaluationEvidence
              result={result}
              fullName={pr.repository_full_name}
            />
          </>
        ) : (
          <p>
            No canonical result is available for the observed revision. Inspect
            a completed historical evaluation above.
          </p>
        )}
      </section>
      {query.data.deferred.length > 0 && (
        <details className="rounded-xl border p-5">
          <summary className="cursor-pointer">
            Deferred evaluation details
          </summary>
          <p className="my-2 text-sm">
            Some orchestration work was deferred. This is separate from the
            persisted evaluation verdict.
          </p>
          <pre className="max-h-64 overflow-auto text-xs break-words whitespace-pre-wrap">
            {JSON.stringify(query.data.deferred, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
