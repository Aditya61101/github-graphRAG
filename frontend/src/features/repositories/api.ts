import type { Repository, RepositoryDto } from "@/features/repositories/types";
import { apiClient } from "@/lib/api-client";

export const repositoryService = {
  getRepositories: async (): Promise<Repository[]> => {
    const response = await apiClient.get<RepositoryDto[]>("/repositories");
    return response.data.map<Repository>((repo) => ({
      id: repo.github_repository_id,
      name: repo.name,
      description: repo.description,
      isPrivate: repo.is_private,
      updatedAt: new Date(repo.updated_at).toLocaleDateString("en-GB"),
    }));
  },
};
