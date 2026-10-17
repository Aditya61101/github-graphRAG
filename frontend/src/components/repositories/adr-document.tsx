import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import { knowledgeService } from "@/api/knowledge-service";
import { getApiErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import type { ADRDocument } from "@/types/knowledge";

export function ADRDocumentEntry({
  document,
  repositoryId,
  scope,
}: {
  document: ADRDocument;
  repositoryId: string;
  scope: (string | null | undefined)[];
}) {
  const client = useQueryClient();
  const request = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState<"check" | "retry" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState<string | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const act = async (action: "check" | "retry") => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
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
      client.setQueryData<ADRDocument[]>(
        ["adr-documents", ...scope],
        (current) =>
          current?.map((item) =>
            item.id === status.adr_id
              ? {
                  ...item,
                  status: status.status,
                  updated_at: status.updated_at,
                  error: status.error,
                }
              : item
          )
      );
      setChecked(new Date().toLocaleTimeString());
      if (status.status === "COMPLETED") {
        await Promise.all(
          [
            "adr-documents",
            "architectural-knowledge",
            "architectural-knowledge-detail",
            "repository-overview",
            "repository-graph",
          ].map((key) =>
            client.invalidateQueries({ queryKey: [key, ...scope] })
          )
        );
      }
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
        <Button
          variant="outline"
          size="sm"
          disabled={busy !== null}
          onClick={() => void act("check")}
        >
          {busy === "check" ? "Checking..." : "Check Status"}
        </Button>
        {document.status === "FAILED" && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy !== null}
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
