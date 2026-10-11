import { apiClient } from "@/lib/api-client";
import type { Repository, RepositoryDto } from "@/types/repositories";
import { mapRepository } from "@/lib/repository-mapper";

export const repositoryService = {
  syncRepository: async (
    repository: Repository
  ): Promise<{ status: string }> => {
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
  getRepositories: async (signal?: AbortSignal): Promise<Repository[]> => {
    const response = await apiClient.get<RepositoryDto[]>("/repositories", {
      signal,
    });
    return response.data.map(mapRepository);
  },
};
