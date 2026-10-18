import axios from "axios";
import { CancelledError, type QueryClient } from "@tanstack/react-query";
import type { ADRDocument } from "@/types/knowledge";
import type { RepositoryOverview } from "@/types/overview";

export type RepositoryScope = readonly (string | null | undefined)[];
type Observation = { document: ADRDocument; completion: string | null };
type ADRState = {
  session: string;
  epoch: number;
  observed: Record<string, Observation>;
  deleted: string[];
  cleanup: string | null;
  uncertain: boolean;
};
const stateKey = (scope: RepositoryScope) => ["adr-reconciliation", ...scope];
const initialState = (): ADRState => ({
  session: crypto.randomUUID(),
  epoch: 0,
  observed: {},
  deleted: [],
  cleanup: null,
  uncertain: false,
});
export const adrStateOptions = (scope: RepositoryScope) => ({
  queryKey: stateKey(scope),
  queryFn: initialState,
  initialData: initialState,
  enabled: false,
  // Session-local bookkeeping must survive source-panel remounts. Logout's
  // QueryClient.clear removes it together with the account's resource caches.
  gcTime: Infinity,
});

export function readADRState(client: QueryClient, scope: RepositoryScope) {
  return client.getQueryData<ADRState>(stateKey(scope));
}

const derivedKeys = [
  "architectural-knowledge",
  "architectural-knowledge-detail",
  "repository-graph",
];
function invalidateDerived(client: QueryClient, scope: RepositoryScope) {
  return Promise.all(
    [...derivedKeys, "repository-overview"].map((key) =>
      client.invalidateQueries({ queryKey: [key, ...scope] })
    )
  );
}

// A response carries the state observed when it started. This guards races even
// when an endpoint does not supply updated_at, or a transport ignores abort.
export function reconcileADRDocuments(
  client: QueryClient,
  scope: RepositoryScope,
  documents: ADRDocument[],
  started: ADRState | undefined
): ADRDocument[] {
  const current = readADRState(client, scope);
  if (
    !current ||
    !started ||
    current.session !== started.session ||
    current.epoch !== started.epoch
  ) {
    return (
      client.getQueryData<ADRDocument[]>(["adr-documents", ...scope]) ?? []
    );
  }
  const observed = { ...current.observed };
  let completed = false;
  const accepted: ADRDocument[] = [];
  for (const document of documents) {
    if (current.deleted.includes(document.id)) continue;
    const previous = observed[document.id];
    const older =
      previous &&
      ((document.updated_at &&
        previous.document.updated_at &&
        Date.parse(document.updated_at) <
          Date.parse(previous.document.updated_at)) ||
        (previous !== started.observed[document.id] &&
          (!document.updated_at ||
            !previous.document.updated_at ||
            Date.parse(document.updated_at) <=
              Date.parse(previous.document.updated_at))));
    if (older) {
      accepted.push(previous.document);
      continue;
    }
    // Some status/list responses omit updated_at. Preserve a known version for
    // an unchanged state rather than treating missing metadata as a new run.
    const acceptedDocument =
      previous?.document.status === document.status && !document.updated_at
        ? { ...document, updated_at: previous.document.updated_at }
        : document;
    const version =
      acceptedDocument.updated_at ?? document.created_at ?? "unversioned";
    const completion = document.status === "COMPLETED" ? version : null;
    if (
      completion !== null &&
      (previous?.document.status !== "COMPLETED" ||
        (document.updated_at &&
          previous.document.updated_at &&
          previous.completion !== completion))
    ) {
      completed = true;
    }
    observed[document.id] = { document: acceptedDocument, completion };
    accepted.push(acceptedDocument);
  }
  client.setQueryData(stateKey(scope), { ...current, observed });
  if (completed) void invalidateDerived(client, scope);
  return accepted;
}

export async function fetchADRDocuments(
  client: QueryClient,
  scope: RepositoryScope,
  fetch: (signal: AbortSignal) => Promise<ADRDocument[]>,
  signal: AbortSignal
) {
  const started = readADRState(client, scope);
  const documents = await fetch(signal);
  if (signal.aborted) throw new CancelledError();
  return reconcileADRDocuments(client, scope, documents, started);
}

export function isGraphReadinessError(error: unknown): boolean {
  return (
    axios.isAxiosError(error) &&
    error.response?.status === 503 &&
    error.response?.data?.detail ===
      "Repository graph update or repair is in progress; retry later"
  );
}

export function canDeleteADR(permissions: {
  can_manage_source_documents: boolean;
  can_view_settings: boolean;
}) {
  // can_view_settings is the existing live GitHub-admin verification result.
  return (
    permissions.can_manage_source_documents === true &&
    permissions.can_view_settings === true
  );
}

export function recordADRDeletion(
  client: QueryClient,
  scope: RepositoryScope,
  document: ADRDocument,
  confirmed = true,
  started = readADRState(client, scope)
) {
  const current = readADRState(client, scope);
  if (!current || !started || current.session !== started.session) return;
  client.setQueryData(stateKey(scope), {
    ...current,
    epoch: current.epoch + 1,
    deleted: confirmed
      ? [...new Set([...current.deleted, document.id])]
      : current.deleted,
    cleanup: document.source_name,
    uncertain: !confirmed,
  });
  // The layout gates every derived view immediately. Keep its active Overview
  // query and recover it explicitly; remove only unsafe feature caches.
  const unsafe = [
    ...derivedKeys,
    "pull-requests",
    "pull-request-detail",
    "evaluation-result",
    "explorer-conversations",
  ];
  void client.cancelQueries({ queryKey: ["adr-documents", ...scope] });
  void client.cancelQueries({ queryKey: ["repository-overview", ...scope] });
  for (const key of unsafe) {
    void client.cancelQueries({ queryKey: [key, ...scope] });
    client.removeQueries({ queryKey: [key, ...scope] });
  }
  if (confirmed)
    client.setQueryData<ADRDocument[]>(["adr-documents", ...scope], (items) =>
      items?.filter((item) => item.id !== document.id)
    );
}

export function finishADRRecovery(
  client: QueryClient,
  scope: RepositoryScope,
  started: ADRState | undefined,
  overview: RepositoryOverview
) {
  const current = readADRState(client, scope);
  if (
    !current ||
    !started ||
    current.session !== started.session ||
    current.epoch !== started.epoch
  )
    return;
  if (current.cleanup) {
    // Publish the verified fresh snapshot before lifting the layout gate. Never
    // expose the retained pre-deletion Overview for even one recovery render.
    client.setQueryData(["repository-overview", ...scope], overview);
    // Newly mounted views will fetch; no independent child recovery loops.
    for (const key of [
      ...derivedKeys,
      "pull-requests",
      "pull-request-detail",
      "evaluation-result",
    ]) {
      void client.invalidateQueries({
        queryKey: [key, ...scope],
        refetchType: "none",
      });
    }
    client.setQueryData(stateKey(scope), {
      ...current,
      cleanup: null,
      uncertain: false,
    });
  }
}
