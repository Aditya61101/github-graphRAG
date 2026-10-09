import { apiClient } from "@/lib/api-client";
import type { Repository, RepositoryDto } from "@/types/repositories";
import { mapRepository } from "@/lib/repository-mapper";

export const repositoryService = {
  getRepositories: async (signal?: AbortSignal): Promise<Repository[]> => {
    const response = await apiClient.get<RepositoryDto[]>("/repositories", {
      signal,
    });
    return response.data.map(mapRepository);
  },
};
