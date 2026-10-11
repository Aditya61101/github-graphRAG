import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Loader, Search, Upload } from "lucide-react";
import { knowledgeService } from "@/api/knowledge-service";
import { useRepositoryOverview } from "@/layouts/repository-layout";
import { useAuth } from "@/contexts/auth-context";
import { getApiErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { KnowledgeDetailModal } from "@/components/repositories/knowledge-detail-modal";
import type { ADRUploadResult, KnowledgeItem } from "@/types/knowledge";

const queryOptions = {
  retry: false,
  // Keep successful responses fresh for five minutes and retain inactive
  // views for fifteen minutes. Keys include the account, session and repo.
  staleTime: 5 * 60_000,
  gcTime: 15 * 60_000,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
} as const;
const entryTitle = (item: KnowledgeItem) =>
  item.title ||
  item.description ||
  (item.kind === "decision" ? "Untitled decision" : "Untitled constraint");

function Failure({ error, retry }: { error: unknown; retry: () => void }) {
  return (
    <div role="alert" className="space-y-3 rounded-xl border p-5">
      <p className="text-sm">
        {getApiErrorMessage(
          error,
          "Unable to load architectural knowledge. Please try again."
        )}
      </p>
      <Button variant="outline" onClick={retry}>
        Try again
      </Button>
    </div>
  );
}

export default function RepositoryDecisionsPage() {
  const queryClient = useQueryClient();
  const overview = useRepositoryOverview();
  const id = overview.repository.id;
  const { user, token } = useAuth();
  const scope = [user?.id, token, id];
  const canManage = overview.permissions.can_manage_source_documents === true;
  const [tab, setTab] = useState<"knowledge" | "sources">("knowledge");
  const [uploading, setUploading] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("all");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const detailTrigger = useRef<HTMLButtonElement>(null);
  const knowledge = useQuery({
    ...queryOptions,
    queryKey: ["architectural-knowledge", ...scope, search, kind, offset],
    queryFn: ({ signal }) =>
      knowledgeService.list(id, search, kind, offset, signal),
  });
  const detail = useQuery({
    ...queryOptions,
    queryKey: ["architectural-knowledge-detail", ...scope, selected],
    queryFn: ({ signal }) => knowledgeService.detail(id, selected!, signal),
    enabled: selected !== null,
  });
  const activeTab = canManage ? tab : "knowledge";
  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Decisions</h1>
        <p className="text-sm text-muted-foreground">
          Extracted architectural decisions, constraints, and their ADR
          evidence.
        </p>
      </section>
      <div
        role="tablist"
        aria-label="Decision views"
        className="flex flex-wrap gap-2 border-b pb-3"
        onKeyDown={(event) => {
          if (
            !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) ||
            uploading
          )
            return;
          const tabs = Array.from(
            event.currentTarget.querySelectorAll<HTMLButtonElement>(
              '[role="tab"]'
            )
          );
          const index = tabs.indexOf(event.target as HTMLButtonElement);
          if (index < 0) return;
          event.preventDefault();
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? tabs.length - 1
                : (index +
                    (event.key === "ArrowRight" ? 1 : -1) +
                    tabs.length) %
                  tabs.length;
          tabs[next].focus();
          tabs[next].click();
        }}
      >
        <Button
          id="knowledge-tab"
          role="tab"
          aria-selected={activeTab === "knowledge"}
          aria-controls="knowledge-panel"
          tabIndex={activeTab === "knowledge" ? 0 : -1}
          disabled={uploading}
          variant={activeTab === "knowledge" ? "secondary" : "ghost"}
          onClick={() => setTab("knowledge")}
        >
          Architectural Knowledge
        </Button>
        {canManage && (
          <Button
            id="sources-tab"
            role="tab"
            aria-selected={activeTab === "sources"}
            aria-controls="sources-panel"
            tabIndex={activeTab === "sources" ? 0 : -1}
            disabled={uploading}
            variant={activeTab === "sources" ? "secondary" : "ghost"}
            onClick={() => setTab("sources")}
          >
            Source Documents
          </Button>
        )}
      </div>
      {activeTab === "sources" ? (
        <SourceDocuments
          repositoryId={id}
          scope={scope}
          onProcessingChange={setUploading}
        />
      ) : (
        <section
          id="knowledge-panel"
          role="tabpanel"
          aria-labelledby="knowledge-tab"
          className="space-y-4"
        >
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              setSearch(searchInput);
              setOffset(0);
            }}
          >
            <div className="min-w-0 flex-1 space-y-2">
              <Label htmlFor="knowledge-search">
                Search architectural knowledge
              </Label>
              <Input
                id="knowledge-search"
                type="search"
                placeholder="Search titles and statements"
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
              />
            </div>
            <Button type="submit" variant="outline">
              <Search aria-hidden="true" />
              Search
            </Button>
          </form>
          <div
            role="group"
            aria-label="Filter architectural knowledge"
            className="flex flex-wrap items-center gap-2"
          >
            {[
              ["all", "All"],
              ["decision", "Decisions"],
              ["constraint", "Constraints"],
            ].map(([value, label]) => (
              <Button
                key={value}
                size="sm"
                variant={kind === value ? "secondary" : "ghost"}
                aria-pressed={kind === value}
                onClick={() => {
                  setKind(value);
                  setOffset(0);
                }}
              >
                {label}
              </Button>
            ))}
            <Button
              size="sm"
              variant="outline"
              className="ml-auto"
              disabled={knowledge.isFetching}
              onClick={() => {
                void queryClient.invalidateQueries({
                  queryKey: ["architectural-knowledge", ...scope],
                });
                void queryClient.invalidateQueries({
                  queryKey: ["architectural-knowledge-detail", ...scope],
                });
              }}
            >
              Refresh knowledge
            </Button>
          </div>
          {knowledge.isError ? (
            <Failure
              error={knowledge.error}
              retry={() => void knowledge.refetch()}
            />
          ) : knowledge.isPending ? (
            <p
              role="status"
              aria-busy="true"
              className="text-sm text-muted-foreground"
            >
              Loading architectural knowledge...
            </p>
          ) : knowledge.data.items.length === 0 ? (
            <div className="rounded-xl border border-dashed p-8 text-center">
              <p className="font-medium">
                {search || kind !== "all"
                  ? "No matching architectural knowledge"
                  : "No extracted architectural knowledge yet"}
              </p>
              <p className="mt-2 text-sm text-muted-foreground">
                {search || kind !== "all"
                  ? "Try another search or filter."
                  : "Decisions and constraints will appear after ADR documents are successfully processed."}
              </p>
            </div>
          ) : (
            <>
              <p role="status" className="text-xs text-muted-foreground">
                Showing {offset + 1}–{offset + knowledge.data.items.length} of{" "}
                {knowledge.data.total} entries
              </p>
              <div className="space-y-3">
                {knowledge.data.items.map((item) => (
                  <article
                    key={item.id}
                    className="rounded-xl border bg-card p-5"
                  >
                    <div className="mb-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
                      <span className="rounded-full border px-2 py-1">
                        {item.kind === "decision" ? "Decision" : "Constraint"}
                      </span>
                      {item.type && (
                        <span className="rounded-full border px-2 py-1">
                          {item.type.replaceAll("_", " ")}
                        </span>
                      )}
                    </div>
                    <Button
                      variant="link"
                      className="h-auto max-w-full justify-start p-0 text-left whitespace-normal"
                      onClick={(event) => {
                        detailTrigger.current = event.currentTarget;
                        setSelected(item.id);
                      }}
                    >
                      <h2 className="line-clamp-2 font-semibold break-words">
                        {entryTitle(item)}
                      </h2>
                    </Button>
                    <p className="mt-2 line-clamp-3 text-sm whitespace-pre-wrap text-muted-foreground">
                      {item.description}
                    </p>
                    <p className="mt-3 text-xs text-muted-foreground">
                      Source ADR:{" "}
                      {item.sources
                        .map(
                          (source) =>
                            source.source_name || source.title || source.id
                        )
                        .join(", ") || "Reference unavailable"}
                    </p>
                  </article>
                ))}
              </div>
              <div className="flex items-center justify-between">
                <Button
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - 20))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  disabled={
                    offset + knowledge.data.items.length >= knowledge.data.total
                  }
                  onClick={() => setOffset(offset + 20)}
                >
                  Next
                </Button>
              </div>
            </>
          )}
        </section>
      )}
      <KnowledgeDetailModal
        open={selected !== null}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
        returnFocus={detailTrigger}
        detail={detail.data}
        error={detail.isError}
        failure={
          <Failure error={detail.error} retry={() => void detail.refetch()} />
        }
      />
    </div>
  );
}

function SourceDocuments({
  repositoryId,
  scope,
  onProcessingChange,
}: {
  repositoryId: string;
  scope: (string | null | undefined)[];
  onProcessingChange: (processing: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const documents = useQuery({
    ...queryOptions,
    queryKey: ["adr-documents", ...scope],
    queryFn: ({ signal }) => knowledgeService.documents(repositoryId, signal),
  });
  const [files, setFiles] = useState<File[]>([]);
  const [processing, setProcessing] = useState(false);
  const [result, setResult] = useState<ADRUploadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const locked = useRef(false);
  const request = useRef<AbortController | null>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => () => request.current?.abort(), []);
  const upload = async () => {
    if (locked.current || !files.length) return;
    locked.current = true;
    setProcessing(true);
    onProcessingChange(true);
    setError(null);
    setResult(null);
    const controller = new AbortController();
    request.current = controller;
    try {
      const response = await knowledgeService.upload(
        repositoryId,
        files,
        controller.signal
      );
      if (!controller.signal.aborted) {
        setResult(response);
        setFiles([]);
        if (input.current) input.current.value = "";
      }
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(
          getApiErrorMessage(
            failure,
            "Upload could not be confirmed. Some files or knowledge may already have been saved."
          )
        );
    } finally {
      // A failed or cancelled HTTP request does not imply a server rollback.
      // Mark both inventories stale even if this panel was unmounted.
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["adr-documents", ...scope],
        }),
        queryClient.invalidateQueries({
          queryKey: ["architectural-knowledge", ...scope],
        }),
        queryClient.invalidateQueries({
          queryKey: ["architectural-knowledge-detail", ...scope],
        }),
      ]);
      if (!controller.signal.aborted) {
        setProcessing(false);
        onProcessingChange(false);
      }
      locked.current = false;
    }
  };
  return (
    <section
      id="sources-panel"
      role="tabpanel"
      aria-labelledby="sources-tab"
      className="space-y-5"
    >
      <div className="space-y-4 rounded-xl border bg-card p-5">
        <h2 className="font-semibold">Upload ADR source documents</h2>
        <p className="text-sm text-muted-foreground">
          Upload Markdown, text, PDF, or Word documents. Processing completes
          within this request.
        </p>
        <Label htmlFor="adr-files">ADR files</Label>
        <Input
          ref={input}
          id="adr-files"
          type="file"
          multiple
          accept=".md,.markdown,.txt,.pdf,.docx"
          disabled={processing}
          onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
        />
        <Button
          disabled={processing || files.length === 0}
          onClick={() => void upload()}
        >
          {processing ? (
            <Loader aria-hidden="true" className="animate-spin" />
          ) : (
            <Upload aria-hidden="true" />
          )}
          {processing ? "Processing documents..." : "Upload documents"}
        </Button>
        {processing && (
          <p
            role="status"
            aria-busy="true"
            className="text-sm text-muted-foreground"
          >
            Extracting architectural knowledge. Keep this page open until
            processing finishes.
          </p>
        )}
        {error && (
          <div role="alert" className="space-y-2 text-sm">
            <p>{error}</p>
            <p className="text-muted-foreground">
              Some data may have been persisted. Review the refreshed document
              status before retrying.
            </p>
          </div>
        )}
        {result && (
          <div
            role={result.failed ? "alert" : "status"}
            className="space-y-2 text-sm"
          >
            <p>
              {result.completed} processed · {result.duplicates} duplicate ·{" "}
              {result.failed} failed
            </p>
            {result.results.map((item) => (
              <p key={item.index}>
                {item.filename}: {item.error || item.status}
              </p>
            ))}
          </div>
        )}
      </div>
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-semibold">Uploaded ADR documents</h2>
        <Button
          variant="outline"
          size="sm"
          disabled={documents.isFetching}
          onClick={() => void documents.refetch()}
        >
          Refresh documents
        </Button>
      </div>
      {documents.isError ? (
        <Failure
          error={documents.error}
          retry={() => void documents.refetch()}
        />
      ) : documents.isPending ? (
        <p role="status">Loading source documents...</p>
      ) : documents.data.length === 0 ? (
        <p className="rounded-xl border border-dashed p-6 text-sm text-muted-foreground">
          No ADR source documents have been uploaded.
        </p>
      ) : (
        <div className="space-y-3">
          {documents.data.map((document) => (
            <article
              key={document.id}
              className="space-y-2 rounded-xl border bg-card p-4"
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
              {document.error && (
                <p className="text-sm text-destructive">{document.error}</p>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
