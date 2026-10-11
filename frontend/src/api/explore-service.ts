import { apiClient } from "@/lib/api-client";
import type { QueryResponse, RepositoryGraph } from "@/types/explore";

const base = (repositoryId: string) =>
  `/repositories/${encodeURIComponent(repositoryId)}`;
export const exploreService = {
  graph: async (
    repositoryId: string,
    signal?: AbortSignal
  ): Promise<RepositoryGraph> =>
    (
      await apiClient.get<RepositoryGraph>(`${base(repositoryId)}/graph`, {
        signal,
      })
    ).data,
  sendMessage: async (
    repositoryId: string,
    conversationId: string,
    query: string,
    signal?: AbortSignal
  ): Promise<QueryResponse> => {
    const response = (
      await apiClient.post<QueryResponse>(
        `${base(repositoryId)}/query`,
        { query, conversation_id: conversationId },
        { signal }
      )
    ).data;
    if (response.conversation_id !== conversationId)
      throw new Error(
        "The response belongs to a different conversation. Please retry."
      );
    return response;
  },
};
