import { apiClient } from "@/lib/api-client";
import type {
  ADRDocument,
  ADRUploadResult,
  KnowledgeDetail,
  KnowledgePage,
} from "@/types/knowledge";

const base = (id: string) => `/repositories/${encodeURIComponent(id)}`;
export const knowledgeService = {
  list: async (
    id: string,
    search: string,
    kind: string,
    offset: number,
    signal?: AbortSignal
  ): Promise<KnowledgePage> =>
    (
      await apiClient.get<KnowledgePage>(`${base(id)}/knowledge`, {
        params: { search, kind, offset, limit: 20 },
        signal,
      })
    ).data,
  detail: async (
    id: string,
    itemId: string,
    signal?: AbortSignal
  ): Promise<KnowledgeDetail> =>
    (
      await apiClient.get<KnowledgeDetail>(
        `${base(id)}/knowledge/${encodeURIComponent(itemId)}`,
        { signal }
      )
    ).data,
  documents: async (id: string, signal?: AbortSignal): Promise<ADRDocument[]> =>
    (await apiClient.get<ADRDocument[]>(`${base(id)}/adrs`, { signal })).data,
  upload: async (
    id: string,
    files: File[],
    signal?: AbortSignal
  ): Promise<ADRUploadResult> => {
    const data = new FormData();
    files.forEach((file) => data.append("files", file));
    return (
      await apiClient.post<ADRUploadResult>(`${base(id)}/adrs`, data, {
        headers: { "Content-Type": undefined },
        signal,
      })
    ).data;
  },
};
