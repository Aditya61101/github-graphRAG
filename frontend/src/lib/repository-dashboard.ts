import axios from "axios";
import type { Repository } from "@/types/repositories";

export const REPOSITORY_FILTERS = [
  ["all", "All"],
  ["ready", "Monitored"],
  ["available", "Available"],
  ["indexing", "Indexing"],
  ["failed", "Failed"],
] as const;
export type RepositoryFilter = (typeof REPOSITORY_FILTERS)[number][0];
export type RepositoryState = Exclude<RepositoryFilter, "all"> | "unknown";

export function getRepositoryState(repository: Repository): RepositoryState {
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
  // /repositories supplies internal IDs only for the current user's records.
  // GitHub discoverability alone does not authorize stored knowledge or admin.
  if (!repository.trackedRepositoryId) return null;
  const state = getRepositoryState(repository);
  return state === "failed" ? "retry" : state === "available" ? "enable" : null;
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
