import axios from "axios";
import type { Repository } from "@/types/repositories";

export const REPOSITORY_FILTERS = [
  ["all", "All"],
  ["ready", "Monitored"],
  ["available", "Available"],
  ["indexing", "Indexing"],
  ["failed", "Failed"],
  ["restricted", "Restricted"],
] as const;
export type RepositoryFilter = (typeof REPOSITORY_FILTERS)[number][0];
export type RepositoryState = Exclude<RepositoryFilter, "all"> | "unknown";

export function getRepositoryState(repository: Repository): RepositoryState {
  const reason = repository.monitoring?.reason;
  // Lifecycle takes precedence over administrative permissions for known owned records.
  if (repository.trackedRepositoryId && repository.status === "COMPLETED")
    return "ready";
  if (repository.trackedRepositoryId && repository.status === "INDEXING")
    return "indexing";
  if (repository.trackedRepositoryId && repository.status === "FAILED")
    return "failed";
  if (
    reason === "owned_by_another_user" ||
    reason === "installation_binding_mismatch" ||
    (reason === "github_admin_required" &&
      ["IDLE", "NOT_INDEXED", "FAILED"].includes(repository.status))
  )
    return "restricted";
  if (!repository.trackedRepositoryId)
    return repository.status === "NOT_INDEXED" ? "available" : "unknown";
  switch (repository.status) {
    case "COMPLETED":
      return "ready";
    case "INDEXING":
      return "indexing";
    case "FAILED":
      return "failed";
    case "IDLE":
    case "NOT_INDEXED":
      return "available";
    default:
      return "unknown";
  }
}

export function getRepositoryAction(
  repository: Repository
): "enable" | "retry" | null {
  const capabilities = repository.monitoring;
  if (capabilities?.github_admin !== true || capabilities.reason !== "allowed")
    return null;
  if (
    ["IDLE", "NOT_INDEXED"].includes(repository.status) &&
    capabilities.can_enable_monitoring === true
  )
    return "enable";
  if (
    repository.trackedRepositoryId &&
    repository.status === "FAILED" &&
    capabilities.can_retry_indexing === true
  )
    return "retry";
  return null;
}

export function uniqueRepositories(repositories: Repository[]): Repository[] {
  const seen = new Set<string>();
  return repositories.filter((repository) => {
    if (seen.has(repository.githubRepositoryId)) return false;
    seen.add(repository.githubRepositoryId);
    return true;
  });
}

export function filterRepositories(
  repositories: Repository[],
  search: string,
  filter: RepositoryFilter
): Repository[] {
  const term = search.trim().toLowerCase();
  return repositories.filter(
    (repository) =>
      repository.fullName.toLowerCase().includes(term) &&
      (filter === "all" || getRepositoryState(repository) === filter)
  );
}

export function needsGitHubReconnect(error: unknown): boolean {
  return (
    axios.isAxiosError<{ detail?: unknown }>(error) &&
    error.response?.status === 401 &&
    error.response.data.detail === "GitHub reconnect required"
  );
}

export function getMonitoringMessage(repository: Repository): string | null {
  switch (repository.monitoring?.reason) {
    case "allowed":
      return null;
    case "github_admin_required":
      return "GitHub repository administrator access is required to enable or retry monitoring.";
    case "owned_by_another_user":
      return "Monitoring is managed by another DecisionGuard user. Stored repository knowledge is not shared.";
    case "installation_binding_mismatch":
      return "The GitHub installation connection needs to be refreshed before monitoring is available.";
    case "permission_unknown":
      return "Monitoring permissions could not be verified. Refresh repositories to try again.";
    default:
      return ["IDLE", "NOT_INDEXED", "FAILED"].includes(repository.status)
        ? "Monitoring permissions could not be verified. Refresh repositories to try again."
        : null;
  }
}

export function getMonitoringActionError(error: unknown): string {
  if (
    axios.isAxiosError<{ detail?: unknown }>(error) &&
    typeof error.response?.data.detail === "string"
  )
    return error.response.data.detail;
  return "Could not start indexing. Please try again.";
}

export function getRepositoryStatusError(error: unknown): string {
  if (
    axios.isAxiosError<{ detail?: unknown }>(error) &&
    typeof error.response?.data.detail === "string"
  )
    return error.response.data.detail;
  return error instanceof Error
    ? error.message
    : "Could not check repository status. Please try again.";
}
