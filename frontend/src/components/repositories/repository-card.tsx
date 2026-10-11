import {
  ExternalLinkIcon,
  GitBranchIcon,
  Loader,
  RefreshCwIcon,
} from "lucide-react";
import {
  Sheet,
  SheetTrigger,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
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
  getMonitoringMessage,
  getRepositoryState,
} from "@/lib/repository-dashboard";
import type { Repository } from "@/types/repositories";

const labels = {
  available: "Available",
  ready: "Monitored",
  indexing: "Indexing",
  failed: "Failed",
  unknown: "Status unavailable",
  restricted: "Restricted",
};
const descriptions = {
  available: "Monitoring has not been activated.",
  ready: null,
  indexing: "Indexing is in progress. Check Status for the latest result.",
  failed: "The latest indexing attempt failed.",
  unknown: "DecisionGuard has not reported a recognized indexing state.",
  restricted: null,
};

type Props = {
  repository: Repository;
  pending: boolean;
  busy?: boolean;
  statusPending?: boolean;
  accepted?: boolean;
  onCheckStatus?: (repository: Repository) => void;
  message?: string;
  error?: string;
  onIndex: (repository: Repository) => void;
};

export function RepositoryCard({
  repository,
  pending,
  busy,
  statusPending = false,
  accepted = false,
  onCheckStatus,
  message,
  error,
  onIndex,
}: Props) {
  const state = getRepositoryState(repository);
  const action = getRepositoryAction(repository);
  const monitoringMessage = getMonitoringMessage(repository);
  const accessDetails =
    monitoringMessage ||
    "GitHub administration and DecisionGuard stored-knowledge access are checked separately. Matching a repository owner name does not grant monitoring rights.";
  const detailsLabel =
    state === "failed"
      ? "View Error"
      : state === "ready" && repository.monitoring?.reason === "allowed"
        ? "View Status"
        : "View Access";
  const owner = repository.fullName.split("/")[0];
  return (
    <Card
      className="h-full min-w-0 flex-col transition-shadow hover:shadow-sm"
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
            <h2 className="line-clamp-2 text-lg font-semibold break-all">
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
        {descriptions[state] &&
          !(state === "available" && monitoringMessage) && (
            <p className="text-sm text-muted-foreground">
              {descriptions[state]}
            </p>
          )}
        {monitoringMessage && state !== "ready" && state !== "indexing" && (
          <p className="text-xs text-muted-foreground">{monitoringMessage}</p>
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
      <CardFooter className="mt-auto flex-wrap justify-between gap-3">
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
        {state === "indexing" || accepted ? (
          <Button
            size="sm"
            variant="outline"
            disabled={busy || statusPending || !onCheckStatus}
            onClick={() => onCheckStatus?.(repository)}
          >
            {statusPending ? (
              <Loader aria-hidden="true" className="animate-spin" />
            ) : (
              <RefreshCwIcon aria-hidden="true" />
            )}
            {statusPending ? "Checking status..." : "Check Status"}
          </Button>
        ) : action ? (
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
                ? "Retry Monitoring"
                : "Enable Monitoring"}
          </Button>
        ) : (
          <Sheet>
            <SheetTrigger render={<Button size="sm" variant="outline" />}>
              {detailsLabel}
            </SheetTrigger>
            <SheetContent>
              <SheetHeader>
                <SheetTitle>{repository.fullName}</SheetTitle>
                <SheetDescription>
                  {state === "failed"
                    ? "Repository indexing details"
                    : "Repository access and status"}
                </SheetDescription>
              </SheetHeader>
              <div className="space-y-4 overflow-y-auto px-4 pb-4">
                <p className="text-sm">Current status: {labels[state]}</p>
                {state === "failed" && (
                  <p className="text-sm text-muted-foreground">
                    The backend reports that indexing failed. Detailed ingestion
                    errors are not exposed by the current repository API.
                  </p>
                )}
                {state === "ready" && (
                  <p className="text-sm text-muted-foreground">
                    The repository is monitored. Open Repository will be
                    available when an authorized repository workspace is
                    connected.
                  </p>
                )}
                <p className="text-sm text-muted-foreground">{accessDetails}</p>
                <p className="text-sm text-muted-foreground">
                  Access to another DecisionGuard user's stored repository
                  knowledge is not granted by GitHub repository access.
                </p>
                {onCheckStatus && (
                  <Button
                    variant="outline"
                    disabled={busy || statusPending}
                    onClick={() => onCheckStatus(repository)}
                  >
                    {statusPending ? "Checking status..." : "Refresh Status"}
                  </Button>
                )}
              </div>
            </SheetContent>
          </Sheet>
        )}
      </CardFooter>
    </Card>
  );
}
