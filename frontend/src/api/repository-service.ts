import { apiClient } from "@/lib/api-client";
import type { Repository, RepositoryDto } from "@/types/repositories";
import { getRepositoryAction } from "@/lib/repository-dashboard";
import { mapRepository } from "@/lib/repository-mapper";

export const repositoryService = {
  enableMonitoring: async (
    repository: Repository
  ): Promise<{ status: string }> => {
    if (getRepositoryAction(repository) !== "enable")
      throw new Error("Monitoring permission has not been verified.");
    if (repository.trackedRepositoryId)
      return repositoryService.syncRepository(repository);
    const response = await apiClient.post<{ status: string }>(
      "/repositories/ingest",
      {
        repository: repository.fullName,
        installation_id: repository.installationId,
        branch: repository.defaultBranch,
        force_full: false,
        sync: false,
      }
    );
    if (
      !["accepted", "completed", "already_indexed"].includes(
        response.data.status
      )
    )
      throw new Error("The server did not accept the indexing request.");
    return response.data;
  },
  syncRepository: async (
    repository: Repository
  ): Promise<{ status: string }> => {
    if (!getRepositoryAction(repository))
      throw new Error("Monitoring permission has not been verified.");
    if (!repository.trackedRepositoryId)
      throw new Error("A tracked repository is required.");
    const [owner, name] = repository.fullName.split("/");
    const response = await apiClient.post<{ status: string }>(
      "/repositories/" +
        encodeURIComponent(owner) +
        "/" +
        encodeURIComponent(name) +
        "/sync",
      { force_full: false, sync: false }
    );
    if (
      !["accepted", "completed", "already_indexed"].includes(
        response.data.status
      )
    )
      throw new Error("The server did not accept the indexing request.");
    return response.data;
  },
  getRepositoryStatus: async (
    repository: Repository,
    signal?: AbortSignal
  ): Promise<Repository> => {
    const repositories = await repositoryService.getRepositories(signal);
    const current = repositories.find(
      (item) => item.githubRepositoryId === repository.githubRepositoryId
    );
    if (!current)
      throw new Error(
        "Repository access is no longer available. Refresh repositories or check GitHub access."
      );
    return current;
  },
  getRepositories: async (signal?: AbortSignal): Promise<Repository[]> => {
    const response = await apiClient.get<RepositoryDto[]>("/repositories", {
      signal,
    });
    return response.data.map(mapRepository);
  },
};
