import {
  ExternalLinkIcon,
  GitBranchIcon,
  Loader,
  RefreshCwIcon,
} from "lucide-react";
import { GithubIcon } from "@/components/icons";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  getRepositoryAction,
  getRepositoryState,
} from "@/lib/repository-dashboard";
import type { Repository } from "@/types/repositories";

const labels = {
  available: "Available",
  ready: "Ready",
  indexing: "Indexing",
  failed: "Failed",
  unknown: "Status unavailable",
};
const descriptions = {
  available:
    "GitHub access is available. Monitoring has not started for your account.",
  ready: "Repository indexing completed successfully.",
  indexing: "Repository indexing is in progress. Status updates automatically.",
  failed: "The latest indexing attempt failed.",
  unknown: "DecisionGuard has not reported a recognized indexing state.",
};

type Props = {
  repository: Repository;
  pending: boolean;
  busy?: boolean;
  message?: string;
  error?: string;
  onIndex: (repository: Repository) => void;
};

export function RepositoryCard({
  repository,
  pending,
  busy,
  message,
  error,
  onIndex,
}: Props) {
  const state = getRepositoryState(repository);
  const action = getRepositoryAction(repository);
  const owner = repository.fullName.split("/")[0];
  return (
    <Card
      className="h-full transition-shadow hover:shadow-sm"
      data-repository-id={repository.githubRepositoryId}
    >
      <CardHeader className="gap-3">
        <div className="flex items-center justify-between gap-2">
          <GithubIcon
            aria-hidden="true"
            className="size-5 text-muted-foreground"
          />
          <span
            className={
              state === "failed"
                ? "rounded-full border border-destructive/30 bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive"
                : "rounded-full border bg-muted/50 px-2 py-0.5 text-xs font-medium text-muted-foreground"
            }
          >
            {state === "indexing" && (
              <Loader
                aria-hidden="true"
                className="mr-1 inline size-3 animate-spin"
              />
            )}
            {labels[state]}
          </span>
        </div>
        <div>
          <CardDescription className="break-all">{owner}</CardDescription>
          <CardTitle>
            <h2 className="text-lg font-semibold break-all">
              {repository.name}
            </h2>
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-4">
        {repository.description && (
          <p className="line-clamp-2 text-sm text-muted-foreground">
            {repository.description}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <span className="rounded-md border px-2 py-1">
            {repository.isPrivate ? "Private" : "Public"}
          </span>
          {(repository.trackedBranch || repository.defaultBranch) && (
            <span className="inline-flex min-w-0 items-center gap-1.5 break-all">
              <GitBranchIcon aria-hidden="true" className="size-3.5 shrink-0" />
              {repository.trackedBranch || repository.defaultBranch}
            </span>
          )}
        </div>
        <p className="text-sm text-muted-foreground">{descriptions[state]}</p>
        {state === "available" && !repository.trackedRepositoryId && (
          <p className="text-xs text-muted-foreground">
            Monitoring permissions are not provided for this repository.
          </p>
        )}
        {message && (
          <p role="status" className="text-sm text-muted-foreground">
            {message}
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
      </CardContent>
      <CardFooter className="flex-wrap justify-between gap-3">
        <Button
          nativeButton={false}
          variant="ghost"
          size="sm"
          render={
            <a
              href={repository.repositoryUrl}
              target="_blank"
              rel="noopener noreferrer"
            />
          }
          aria-label={"View " + repository.fullName + " on GitHub"}
        >
          View on GitHub <ExternalLinkIcon aria-hidden="true" />
        </Button>
        {action && (
          <Button
            size="sm"
            variant={action === "retry" ? "outline" : "default"}
            disabled={pending || busy}
            onClick={() => onIndex(repository)}
          >
            {pending ? (
              <Loader aria-hidden="true" className="animate-spin" />
            ) : action === "retry" ? (
              <RefreshCwIcon aria-hidden="true" />
            ) : null}
            {pending
              ? "Requesting indexing..."
              : action === "retry"
                ? "Retry indexing"
                : "Enable monitoring"}
          </Button>
        )}
      </CardFooter>
    </Card>
  );
}
