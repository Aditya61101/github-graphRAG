import { useEffect, useRef, useState } from "react";
import {
  useMutation,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { repositoryService } from "@/api/repository-service";
import { RepositoryCard } from "./repository-card";
import {
  getMonitoringActionError,
  getRepositoryStatusError,
  getRepositoryAction,
  getRepositoryState,
} from "@/lib/repository-dashboard";
import type { Repository } from "@/types/repositories";

type Props = { repository: Repository; queryKey: QueryKey };

export function RepositoryCardActions({ repository, queryKey }: Props) {
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<{ message?: string; error?: string }>(
    {}
  );
  const [accepted, setAccepted] = useState(false);
  const request = useRef<AbortController | null>(null);
  const submitting = useRef(false);
  useEffect(
    () => () => {
      request.current?.abort();
    },
    []
  );
  const lifecycle = getRepositoryState(repository);
  useEffect(() => {
    if (["indexing", "ready", "failed"].includes(lifecycle)) {
      setAccepted(false);
      setNotice((previous) => ({ error: previous.error }));
    }
  }, [lifecycle]);
  const update = (current: Repository) => {
    queryClient.setQueryData<Repository[]>(queryKey, (previous) =>
      previous?.map((item) =>
        item.githubRepositoryId === current.githubRepositoryId ? current : item
      )
    );
  };
  const operation = useMutation({
    mutationFn: async (kind: "index" | "status") => {
      const controller = new AbortController();
      request.current = controller;
      setNotice({});
      let scheduled = false;
      try {
        if (kind === "index") {
          if (!getRepositoryAction(repository))
            throw new Error(
              "Monitoring is not authorized for this repository."
            );
          const result =
            getRepositoryAction(repository) === "enable"
              ? await repositoryService.enableMonitoring(repository)
              : await repositoryService.syncRepository(repository);
          if (controller.signal.aborted) return;
          scheduled = result.status === "accepted";
          setAccepted(scheduled);
          setNotice({
            message:
              result.status === "accepted"
                ? "Indexing request accepted. Checking status..."
                : undefined,
          });
        }
        const current = await repositoryService.getRepositoryStatus(
          repository,
          controller.signal
        );
        if (controller.signal.aborted) return;
        update(current);
        const state = getRepositoryState(current);
        setAccepted((previous) =>
          state === "available" || state === "restricted" || state === "unknown"
            ? previous
            : false
        );
        setNotice({
          message:
            kind === "index" && state === "available"
              ? "Indexing request accepted. Check Status to see whether indexing has started."
              : undefined,
        });
      } catch (error) {
        if (!controller.signal.aborted)
          setNotice((previous) => ({
            ...previous,
            message: scheduled
              ? "Indexing request accepted. Check Status to retry verification."
              : previous.message,
            error:
              kind === "index" && !scheduled
                ? getMonitoringActionError(error)
                : getRepositoryStatusError(error),
          }));
      } finally {
        submitting.current = false;
      }
    },
  });
  const run = (kind: "index" | "status") => {
    if (submitting.current) return;
    submitting.current = true;
    operation.mutate(kind);
  };
  return (
    <RepositoryCard
      repository={repository}
      pending={operation.isPending && operation.variables === "index"}
      statusPending={
        operation.isPending && (operation.variables === "status" || accepted)
      }
      busy={operation.isPending}
      accepted={accepted}
      message={notice.message}
      error={notice.error}
      onIndex={() => run("index")}
      onCheckStatus={() => run("status")}
    />
  );
}
