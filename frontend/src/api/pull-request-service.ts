import { apiClient } from "@/lib/api-client";
import { parseDetail, parsePage, parseResult } from "@/lib/evaluation-contract";

const base = (id: string) => {
  if (!/^repo_[A-Za-z0-9_-]+$/.test(id))
    throw new Error("Invalid repository ID");
  return `/repositories/${encodeURIComponent(id)}`;
};
export const pullRequestService = {
  async list(id: string, offset = 0, signal?: AbortSignal) {
    const data = parsePage(
      (
        await apiClient.get(`${base(id)}/pull-requests`, {
          params: { limit: 20, offset },
          signal,
        })
      ).data
    );
    if (data.items.some((pr) => pr.repository_id !== id))
      throw new Error("Repository response mismatch");
    return data;
  },
  async detail(id: string, number: number, offset = 0, signal?: AbortSignal) {
    if (!Number.isSafeInteger(number) || number <= 0)
      throw new Error("Invalid pull request number");
    const data = parseDetail(
      (
        await apiClient.get(`${base(id)}/pull-requests/${number}`, {
          params: { limit: 20, offset },
          signal,
        })
      ).data
    );
    if (
      data.pull_request.repository_id !== id ||
      data.pull_request.pr_number !== number ||
      (data.result &&
        (data.result.revision.repository_id !== id ||
          data.result.revision.pull_request_number !== number ||
          data.result.revision.head_sha !==
            data.pull_request.evaluation?.head_sha ||
          data.result.revision.base_sha !==
            data.pull_request.evaluation?.base_sha))
    )
      throw new Error("Pull request response mismatch");
    return data;
  },
  async result(id: string, job: string, signal?: AbortSignal) {
    const data = parseResult(
      (
        await apiClient.get(
          `${base(id)}/evaluations/${encodeURIComponent(job)}/result`,
          { signal }
        )
      ).data
    );
    if (data.revision.repository_id !== id)
      throw new Error("Evaluation repository mismatch");
    return data;
  },
};
