import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { pullRequestService } from "@/api/pull-request-service";
import { useAuth } from "@/contexts/auth-context";
import { useRepositoryOverview } from "@/layouts/repository-layout";
import {
  Pagination,
  ReviewFailure,
} from "@/components/repositories/review-summary";
import { PullRequestTable } from "@/components/repositories/pull-request-table";
import {
  ContentLoading,
  RefreshButton,
} from "@/components/core/query-feedback";
import { isAuthoritativeAccessError } from "@/lib/api-error";

export default function RepositoryPullRequestsPage() {
  const { repository } = useRepositoryOverview();
  return <PullRequests key={repository.id} id={repository.id} />;
}
function PullRequests({ id }: { id: string }) {
  const { user, token } = useAuth();
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["pull-requests", user?.id, token, id, offset],
    queryFn: ({ signal }) => pullRequestService.list(id, offset, signal),
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Pull Requests</h1>
        <p className="text-sm text-muted-foreground">
          Architectural reviews of pull requests in this repository.
        </p>
      </header>
      <RefreshButton
        busy={query.isFetching}
        label="Refresh reviews"
        onRefresh={() => {
          void query.refetch({ cancelRefetch: false });
        }}
      />
      {query.isError &&
        query.data &&
        !isAuthoritativeAccessError(query.error) && (
          <p role="alert" className="text-sm text-muted-foreground">
            Unable to refresh reviews. Previously loaded reviews are shown. Try
            Refresh reviews again.
          </p>
        )}
      {query.isPending ? (
        <ContentLoading label="Loading pull requests..." />
      ) : query.isError &&
        (!query.data || isAuthoritativeAccessError(query.error)) ? (
        <ReviewFailure
          error={query.error}
          retry={() => void query.refetch({ cancelRefetch: false })}
        />
      ) : (
        <>
          {query.data!.items.length ? (
            <PullRequestTable items={query.data!.items} />
          ) : (
            <p className="rounded-xl border border-dashed p-8">
              No observed pull requests on this page. Reviews appear after a
              pull request is observed or evaluated.
            </p>
          )}
          <Pagination
            offset={offset}
            limit={query.data!.limit}
            count={query.data!.items.length}
            total={query.data!.total}
            onPage={setOffset}
          />
        </>
      )}
    </div>
  );
}
