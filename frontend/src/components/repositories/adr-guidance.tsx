import { useEffect, useId } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { FileText, X } from "lucide-react";
import { knowledgeService } from "@/api/knowledge-service";
import { useAuth } from "@/contexts/auth-context";
import { useRepositoryOverview } from "@/layouts/repository-layout";
import { Button } from "@/components/ui/button";
import {
  adrStateOptions,
  fetchADRDocuments,
  readADRState,
} from "@/lib/adr-reconciliation";
import type { ADRDocument } from "@/types/knowledge";
import {
  adrGuidanceKey,
  claimADRGuidance,
  hasSeenADRGuidance,
  isADRGuidanceEligible,
} from "@/lib/adr-guidance";

type Presentation = { seen: boolean; owner: string | null };

export function RepositoryADRGuidance() {
  const { user, token } = useAuth();
  const { repository, permissions } = useRepositoryOverview();
  if (!user || !token) return null;
  const storageKey = adrGuidanceKey(user.id, repository.id);
  if (!storageKey) return null;
  return (
    <ScopedADRGuidance
      key={storageKey}
      storageKey={storageKey}
      scope={[user.id, token, repository.id]}
      status={repository.status}
      canManage={permissions.can_manage_source_documents === true}
    />
  );
}

function ScopedADRGuidance({
  storageKey,
  scope,
  status,
  canManage,
}: {
  storageKey: string;
  scope: string[];
  status: string;
  canManage: boolean;
}) {
  const client = useQueryClient();
  const owner = useId();
  const [userId, token, repositoryId] = scope;
  const adrState = useQuery(adrStateOptions(scope));
  // A local, non-network query keeps presentation ownership stable under
  // StrictMode. Persisted seen status survives hard refresh via localStorage.
  const presentation = useQuery({
    queryKey: ["adr-guidance-presentation", storageKey],
    queryFn: (): Presentation => ({
      seen: hasSeenADRGuidance(storageKey),
      owner: null,
    }),
    initialData: (): Presentation => ({
      seen: hasSeenADRGuidance(storageKey),
      owner: null,
    }),
    enabled: false,
    gcTime: Infinity,
  });
  const mayCheck =
    status === "COMPLETED" &&
    canManage &&
    adrState.data.epoch === 0 &&
    !adrState.data.cleanup &&
    !presentation.data.seen;
  const documents = useQuery({
    queryKey: ["adr-documents", ...scope],
    queryFn: ({ signal }) =>
      fetchADRDocuments(
        client,
        scope,
        (requestSignal) => knowledgeService.documents(scope[2], requestSignal),
        signal
      ),
    enabled: mayCheck,
    retry: false,
    staleTime: 0,
    gcTime: 15 * 60_000,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const eligible = isADRGuidanceEligible({
    status,
    canManage,
    inventory: documents.data,
    succeeded: documents.isSuccess,
    fetching: documents.isFetching,
    deletionEpoch: adrState.data.epoch,
    cleanup: Boolean(adrState.data.cleanup),
  });
  useEffect(() => {
    const queryKey = ["adr-guidance-presentation", storageKey];
    if (!eligible || client.getQueryData<Presentation>(queryKey)?.seen) return;
    // Recheck live cache state before the side effect: a deletion may have
    // started after the eligible render but before passive effects flush.
    const currentScope = [userId, token, repositoryId];
    const current = readADRState(client, currentScope);
    const inventory = client.getQueryState<ADRDocument[]>([
      "adr-documents",
      ...currentScope,
    ]);
    if (
      !current ||
      current.epoch !== 0 ||
      current.cleanup ||
      inventory?.status !== "success" ||
      inventory.fetchStatus !== "idle" ||
      inventory.data?.length !== 0
    )
      return;
    const claimed = claimADRGuidance(storageKey);
    client.setQueryData<Presentation>(queryKey, {
      seen: true,
      owner: claimed ? owner : null,
    });
  }, [eligible, client, storageKey, owner, userId, token, repositoryId]);

  if (!eligible || presentation.data.owner !== owner) return null;
  return (
    <section
      aria-labelledby="adr-guidance-title"
      className="relative flex flex-col gap-4 rounded-xl border bg-card p-5 pr-14 sm:flex-row sm:items-center"
    >
      <FileText
        aria-hidden="true"
        className="size-6 shrink-0 text-primary dark:text-sidebar-primary"
      />
      <div className="min-w-0 flex-1 space-y-2">
        <h2 id="adr-guidance-title" className="font-semibold">
          Repository indexed successfully
        </h2>
        <p className="text-sm leading-6 text-muted-foreground">
          Your codebase is ready to explore. Upload an Architecture Decision
          Record (ADR) to provide explicit architectural decisions for
          DecisionGuard to monitor.
        </p>
        <Button
          nativeButton={false}
          size="sm"
          render={
            <Link
              to={`/projects/${encodeURIComponent(scope[2])}/decisions?tab=sources`}
            />
          }
        >
          Upload an ADR
        </Button>
      </div>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Dismiss ADR upload guidance"
        className="absolute top-3 right-3"
        onClick={() =>
          client.setQueryData<Presentation>(
            ["adr-guidance-presentation", storageKey],
            { seen: true, owner: null }
          )
        }
      >
        <X aria-hidden="true" />
      </Button>
    </section>
  );
}
