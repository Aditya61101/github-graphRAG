import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import { Dialog } from "@base-ui/react/dialog";
import axios from "axios";
import { knowledgeService } from "@/api/knowledge-service";
import { getApiErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import type { ADRDocument } from "@/types/knowledge";
import {
  readADRState,
  reconcileADRDocuments,
  recordADRDeletion,
} from "@/lib/adr-reconciliation";

export function ADRDocumentEntry({
  document,
  repositoryId,
  scope,
  canDelete = false,
  onDeleted,
}: {
  document: ADRDocument;
  repositoryId: string;
  scope: (string | null | undefined)[];
  canDelete?: boolean;
  onDeleted?: (name: string) => void;
}) {
  const client = useQueryClient();
  const request = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState<"check" | "retry" | "delete" | null>(null);
  const [confirming, setConfirming] = useState(false);
  const deleteTrigger = useRef<HTMLButtonElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState<string | null>(null);
  const remove = async () => {
    if (!canDelete || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy("delete");
    setError(null);
    const started = readADRState(client, scope);
    try {
      await knowledgeService.deleteDocument(
        repositoryId,
        document.id,
        controller.signal
      );
      if (!controller.signal.aborted) onDeleted?.(document.source_name);
      recordADRDeletion(client, scope, document, true, started);
    } catch (failure) {
      if (!controller.signal.aborted) {
        setConfirming(false);
        const ambiguous =
          !axios.isAxiosError(failure) ||
          !failure.response ||
          failure.response.status >= 500;
        if (ambiguous) {
          // A lost response can follow a committed delete. Hide potentially
          // obsolete evidence until the layout verifies repository readiness.
          recordADRDeletion(client, scope, document, false, started);
        }
        setError(
          getApiErrorMessage(
            failure,
            "Deletion could not be confirmed. Refresh documents before retrying; the server may already have saved the deletion."
          )
        );
      }
    } finally {
      if (controller.signal.aborted && !readADRState(client, scope)?.cleanup) {
        recordADRDeletion(client, scope, document, false, started);
      }
      if (!controller.signal.aborted) {
        setBusy(null);
        request.current = null;
      }
    }
  };
  useEffect(() => () => request.current?.abort(), []);
  const act = async (action: "check" | "retry") => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    const started = readADRState(client, scope);
    setBusy(action);
    setError(null);
    try {
      const status = await knowledgeService[
        action === "check" ? "status" : "retry"
      ](repositoryId, document.id, controller.signal);
      if (controller.signal.aborted) return;
      if (
        status.adr_id !== document.id ||
        !["PENDING", "PROCESSING", "COMPLETED", "FAILED"].includes(
          status.status
        )
      )
        throw new Error("Invalid ADR status response.");
      await client.cancelQueries({ queryKey: ["adr-documents", ...scope] });
      if (controller.signal.aborted) return;
      const entries = reconcileADRDocuments(
        client,
        scope,
        [
          {
            ...document,
            status: status.status,
            updated_at: status.updated_at,
            error: status.error,
          },
        ],
        started
      );
      const latest = entries.find((item) => item.id === document.id);
      client.setQueryData<ADRDocument[]>(
        ["adr-documents", ...scope],
        (current) =>
          current?.map((item) =>
            item.id === status.adr_id && latest ? latest : item
          )
      );
      setChecked(new Date().toLocaleTimeString());
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(
          getApiErrorMessage(
            failure,
            action === "retry"
              ? "Retry could not be confirmed. Check Status before trying again."
              : "Could not check ADR status. Please try again."
          )
        );
    } finally {
      if (!controller.signal.aborted) {
        setBusy(null);
        request.current = null;
      }
    }
  };
  return (
    <article
      className="space-y-2 rounded-xl border bg-card p-4"
      aria-label={document.source_name}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="font-medium break-all">
          <FileText aria-hidden="true" className="mr-2 inline size-4" />
          {document.source_name}
        </h3>
        <span className="rounded-full border px-2 py-1 text-xs">
          {document.status}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        {document.source_type} · Uploaded{" "}
        {document.created_at
          ? new Date(document.created_at).toLocaleString()
          : "date unavailable"}
      </p>
      {document.status === "PENDING" && (
        <p className="text-sm text-muted-foreground">
          Queued for processing. Check Status when ready.
        </p>
      )}
      {document.status === "PROCESSING" && (
        <p className="text-sm text-muted-foreground">
          Processing in the background.
        </p>
      )}
      {document.error && (
        <p className="text-sm text-destructive">{document.error}</p>
      )}
      <div className="flex gap-2">
        {canDelete && (
          <>
            <Button
              ref={deleteTrigger}
              variant="destructive"
              size="sm"
              disabled={busy !== null}
              aria-busy={busy !== null}
              aria-haspopup="dialog"
              aria-expanded={confirming}
              onClick={() => setConfirming(true)}
            >
              Delete
            </Button>
            <Dialog.Root
              open={confirming}
              onOpenChange={(open) => {
                if (busy !== "delete") setConfirming(open);
              }}
            >
              <Dialog.Portal>
                <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/30" />
                <Dialog.Popup
                  finalFocus={deleteTrigger}
                  aria-busy={busy === "delete"}
                  className="fixed top-1/2 left-1/2 z-50 w-[min(28rem,calc(100%_-_2rem))] -translate-x-1/2 -translate-y-1/2 space-y-4 rounded-xl border bg-card p-6 shadow-xl"
                >
                  <Dialog.Title className="font-semibold">
                    Delete ADR?
                  </Dialog.Title>
                  <Dialog.Description className="text-sm text-muted-foreground">
                    Delete {document.source_name}? It will be removed from
                    future architectural reviews.
                  </Dialog.Description>
                  <div className="flex justify-end gap-2">
                    <Button
                      variant="outline"
                      disabled={busy === "delete"}
                      onClick={() => setConfirming(false)}
                    >
                      Cancel
                    </Button>
                    <Button
                      variant="destructive"
                      disabled={busy === "delete"}
                      onClick={() => void remove()}
                    >
                      {busy === "delete" ? "Deleting..." : "Delete ADR"}
                    </Button>
                  </div>
                </Dialog.Popup>
              </Dialog.Portal>
            </Dialog.Root>
          </>
        )}
        <Button
          variant="outline"
          size="sm"
          disabled={busy !== null}
          aria-busy={busy !== null}
          onClick={() => void act("check")}
        >
          {busy === "check" ? "Checking status…" : "Check Status"}
        </Button>
        {document.status === "FAILED" && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy !== null}
            aria-busy={busy !== null}
            onClick={() => void act("retry")}
          >
            {busy === "retry" ? "Retrying..." : "Retry"}
          </Button>
        )}
      </div>
      {checked && (
        <p className="text-xs text-muted-foreground">Last checked {checked}</p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </article>
  );
}
