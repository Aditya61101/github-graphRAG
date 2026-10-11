import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  GitForkIcon,
  Loader,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
} from "lucide-react";
import { authService } from "@/api/auth-service";
import { repositoryService } from "@/api/repository-service";
import { RepositoryHeader } from "@/components/repositories/repository-header";
import { RepositoryCard } from "@/components/repositories/repository-card";
import { EmptyState } from "@/components/core/feedback/empty-state";
import { ErrorState } from "@/components/core/feedback/error-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/contexts/auth-context";
import { getApiErrorMessage } from "@/lib/api-error";
import { getOnboardingError } from "@/lib/onboarding-flow";
import {
  filterRepositories,
  getRepositoryAction,
  getRepositoryState,
  needsGitHubReconnect,
  REPOSITORY_FILTERS,
  uniqueRepositories,
  type RepositoryFilter,
} from "@/lib/repository-dashboard";
import type { Repository } from "@/types/repositories";

export default function ProjectsPage() {
  const { token, user, status } = useAuth();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<RepositoryFilter>("all");
  const [notice, setNotice] = useState<{
    id: string;
    message?: string;
    error?: string;
  } | null>(null);
  const discovery = useQuery({
    queryKey: ["repositories", user?.id, token],
    queryFn: ({ signal }) => repositoryService.getRepositories(signal),
    enabled: Boolean(user && token),
    retry: false,
    staleTime: 0,
    // Poll active indexing, without treating an accepted request as an indexing state.
    refetchInterval: (query) =>
      !query.state.error &&
      query.state.data?.some(
        (repository) => getRepositoryState(repository) === "indexing"
      )
        ? 10_000
        : false,
  });
  const indexing = useMutation({
    mutationFn: async (repository: Repository) => {
      if (!getRepositoryAction(repository))
        throw new Error("Monitoring is not authorized for this repository.");
      return repositoryService.syncRepository(repository);
    },
    onMutate: () => setNotice(null),
    onSuccess: (result, repository) => {
      setNotice({
        id: repository.githubRepositoryId,
        message:
          result.status === "accepted"
            ? "Indexing request accepted. Refresh to check whether indexing has started."
            : "Indexing completed. Refreshing status.",
      });
      void discovery.refetch();
    },
    onError: (error, repository) => {
      setNotice({
        id: repository.githubRepositoryId,
        error: getApiErrorMessage(
          error,
          "Could not start indexing. Please try again."
        ),
      });
      void discovery.refetch();
    },
  });
  const repositories = uniqueRepositories(discovery.data ?? []);
  const visible = filterRepositories(repositories, search, filter);
  const failure = discovery.error ? getOnboardingError(discovery.error) : null;
  const reconnect =
    status?.reconnect_required ||
    failure?.outcome === "reauthorization_required" ||
    needsGitHubReconnect(discovery.error);
  const refresh = () => {
    setNotice(null);
    void discovery.refetch();
  };
  const connectButton = (
    <Button nativeButton={false} render={<Link to="/install" />}>
      <PlusIcon aria-hidden="true" /> Connect repositories
    </Button>
  );

  return (
    <div className="min-h-svh bg-background">
      <RepositoryHeader />
      <main className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6 lg:px-8 lg:py-10">
        <section className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-2">
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              Your repositories
            </h1>
            <p className="max-w-2xl text-sm text-muted-foreground">
              Find the public repositories accessible to your GitHub account and
              check their DecisionGuard indexing status.
            </p>
          </div>
          {connectButton}
        </section>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="w-full space-y-2 sm:max-w-md">
            <Label htmlFor="repository-search">Search repositories</Label>
            <div className="relative">
              <SearchIcon
                aria-hidden="true"
                className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
              />
              <Input
                id="repository-search"
                className="pl-9"
                placeholder="Search by name or owner"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                type="search"
              />
            </div>
          </div>
          <Button
            variant="outline"
            onClick={refresh}
            disabled={discovery.isFetching}
          >
            <RefreshCwIcon
              aria-hidden="true"
              className={discovery.isFetching ? "animate-spin" : undefined}
            />
            {discovery.isFetching ? "Refreshing..." : "Refresh repositories"}
          </Button>
        </div>
        <div
          role="group"
          aria-label="Filter repositories by status"
          className="flex flex-wrap gap-2"
        >
          {REPOSITORY_FILTERS.map(([value, label]) => (
            <Button
              key={value}
              variant={filter === value ? "secondary" : "ghost"}
              size="sm"
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {label}
            </Button>
          ))}
        </div>
        {reconnect && (
          <div
            role="alert"
            className="flex flex-col gap-3 rounded-xl border bg-muted/50 p-4 sm:flex-row sm:items-center sm:justify-between"
          >
            <p className="text-sm">
              Reconnect your GitHub account to refresh repository access. Your
              completed onboarding is preserved.
            </p>
            <Button variant="outline" onClick={authService.loginWithGithub}>
              Reconnect GitHub
            </Button>
          </div>
        )}
        {discovery.isPending ? (
          <section
            aria-label="Loading repositories"
            aria-busy="true"
            className="space-y-4"
          >
            <p
              role="status"
              className="flex items-center gap-2 text-sm text-muted-foreground"
            >
              <Loader aria-hidden="true" className="size-4 animate-spin" />{" "}
              Discovering repositories...
            </p>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {Array.from({ length: 6 }, (_, i) => (
                <div key={i} className="space-y-4 rounded-xl border p-5">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-6 w-40" />
                  <Skeleton className="h-16 w-full" />
                  <Skeleton className="h-8 w-28" />
                </div>
              ))}
            </div>
          </section>
        ) : failure ? (
          <section
            role="alert"
            className="space-y-4 rounded-xl border p-6 sm:p-10"
          >
            <ErrorState
              text="Unable to refresh repositories"
              subtext={failure.message}
            />
            <p className="text-center text-sm text-muted-foreground">
              Repository access could not be verified. Try again or manage your
              GitHub installation.
            </p>
            <div className="flex flex-wrap justify-center gap-3">
              <Button
                variant="outline"
                onClick={refresh}
                disabled={discovery.isFetching}
              >
                Try again
              </Button>
              {connectButton}
            </div>
          </section>
        ) : repositories.length === 0 ? (
          <section className="rounded-xl border border-dashed px-6 py-16">
            <EmptyState
              Icon={GitForkIcon}
              text="No accessible repositories"
              subtext="Connect public repositories through the DecisionGuard GitHub App to see them here."
              actionNode={connectButton}
            />
          </section>
        ) : (
          <section className="space-y-4" aria-label="Repositories">
            {!repositories.some(
              (repository) => getRepositoryState(repository) === "ready"
            ) && (
              <p className="text-sm text-muted-foreground">
                No repositories are ready yet. Each card shows its current
                indexing state.
              </p>
            )}
            <p role="status" className="text-sm text-muted-foreground">
              {visible.length}{" "}
              {visible.length === 1 ? "repository" : "repositories"}
              {search || filter !== "all"
                ? " matching your filters"
                : " accessible to your account"}
            </p>
            {visible.length ? (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {visible.map((repository) => (
                  <RepositoryCard
                    key={repository.githubRepositoryId}
                    repository={repository}
                    busy={
                      indexing.isPending ||
                      (notice?.id === repository.githubRepositoryId &&
                        Boolean(notice.message))
                    }
                    pending={
                      indexing.isPending &&
                      indexing.variables.githubRepositoryId ===
                        repository.githubRepositoryId
                    }
                    message={
                      notice?.id === repository.githubRepositoryId
                        ? notice.message
                        : undefined
                    }
                    error={
                      notice?.id === repository.githubRepositoryId
                        ? notice.error
                        : undefined
                    }
                    onIndex={(repository) => indexing.mutate(repository)}
                  />
                ))}
              </div>
            ) : (
              <div className="rounded-xl border border-dashed px-6 py-12">
                <EmptyState
                  text="No matching repositories"
                  subtext="Try another name, owner, or status filter."
                  actionNode={
                    <Button
                      variant="outline"
                      onClick={() => {
                        setSearch("");
                        setFilter("all");
                      }}
                    >
                      Clear filters
                    </Button>
                  }
                />
              </div>
            )}
          </section>
        )}
      </main>
    </div>
  );
}
