import { useRef, useState, type RefObject, type ReactNode } from "react";
import { Dialog } from "@base-ui/react/dialog";
import { ChevronDown, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { KnowledgeDetail } from "@/types/knowledge";

type ModalProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  returnFocus: RefObject<HTMLButtonElement | null>;
  detail?: KnowledgeDetail;
  error: boolean;
  failure: ReactNode;
};

export function KnowledgeDetailModal({
  open,
  onOpenChange,
  returnFocus,
  detail,
  error,
  failure,
}: ModalProps) {
  const title = useRef<HTMLHeadingElement>(null);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/30 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup
          data-slot="knowledge-detail-modal"
          initialFocus={title}
          finalFocus={returnFocus}
          className="fixed top-0 left-0 z-50 flex h-dvh max-h-dvh w-full flex-col overflow-hidden bg-popover text-popover-foreground shadow-xl md:top-1/2 md:left-1/2 md:h-auto md:max-h-[90vh] md:w-[min(900px,calc(100vw-2rem))] md:-translate-x-1/2 md:-translate-y-1/2 md:rounded-2xl md:border"
        >
          <header className="relative shrink-0 space-y-2 border-b p-5 pr-16 md:px-6 md:pr-16">
            <Dialog.Title
              ref={title}
              tabIndex={-1}
              className="line-clamp-3 text-xl font-semibold tracking-tight break-words outline-none"
            >
              {!error && detail
                ? detail.title ||
                  (detail.kind === "decision"
                    ? "Architectural decision"
                    : "Architectural constraint")
                : "Architectural knowledge"}
            </Dialog.Title>
            <Dialog.Description className="text-sm text-muted-foreground">
              Documented ADR knowledge and supporting provenance.
            </Dialog.Description>
            <Dialog.Close
              render={
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="absolute top-4 right-4"
                />
              }
            >
              <X aria-hidden="true" />
              <span className="sr-only">Close architectural knowledge</span>
            </Dialog.Close>
          </header>
          <div
            data-slot="knowledge-detail-scroll"
            className="min-h-0 overflow-y-auto overscroll-contain p-5 md:p-6"
          >
            {error ? (
              failure
            ) : !detail ? (
              <p role="status">Loading entry...</p>
            ) : (
              <KnowledgeDetailBody key={detail.id} detail={detail} />
            )}
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function KnowledgeDetailBody({ detail }: { detail: KnowledgeDetail }) {
  const [fullStatement, setFullStatement] = useState(false);
  const [allEntities, setAllEntities] = useState(false);
  const longStatement = detail.description.length > 600;
  const grouped = new Map<
    string,
    {
      id: string;
      name: string | null;
      labels: Set<string>;
      relationships: Set<KnowledgeDetail["entities"][number]["relationship"]>;
    }
  >();
  for (const entity of detail.entities) {
    const row = grouped.get(entity.id) ?? {
      id: entity.id,
      name: entity.name,
      labels: new Set<string>(),
      relationships: new Set<
        KnowledgeDetail["entities"][number]["relationship"]
      >(),
    };
    row.name ||= entity.name;
    if (entity.label) row.labels.add(entity.label);
    row.relationships.add(entity.relationship);
    grouped.set(entity.id, row);
  }
  const entities = Array.from(grouped.values());
  const sources = new Map(detail.sources.map((source) => [source.id, source]));
  return (
    <div
      data-slot="knowledge-detail-columns"
      className="grid min-w-0 gap-6 md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]"
    >
      <div className="min-w-0 space-y-6">
        <section
          aria-labelledby="knowledge-statement-heading"
          className="space-y-3"
        >
          <h3 id="knowledge-statement-heading" className="font-semibold">
            Complete statement
          </h3>
          <p
            id="knowledge-complete-statement"
            className={`text-sm leading-7 wrap-anywhere whitespace-pre-wrap ${longStatement && !fullStatement ? "line-clamp-8" : ""}`}
          >
            {detail.description}
          </p>
          {longStatement && (
            <Button
              variant="outline"
              size="sm"
              aria-expanded={fullStatement}
              aria-controls="knowledge-complete-statement"
              onClick={() => setFullStatement(!fullStatement)}
            >
              {fullStatement ? "Show less statement" : "Show full statement"}
            </Button>
          )}
        </section>
        <section
          aria-labelledby="knowledge-evidence-heading"
          className="space-y-3 border-t pt-5"
        >
          <h3 id="knowledge-evidence-heading" className="font-semibold">
            Supporting evidence{" "}
            <span className="font-normal text-muted-foreground">
              ({detail.evidence.length})
            </span>
          </h3>
          {detail.evidence.length ? (
            detail.evidence.map((evidence, index) => {
              const source = sources.get(evidence.adr_id);
              return (
                <details
                  key={evidence.id}
                  open={index < 3}
                  className="group rounded-xl border bg-muted/20"
                >
                  <summary className="flex cursor-pointer list-none items-start gap-2 rounded-xl p-3 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none [&::-webkit-details-marker]:hidden">
                    <ChevronDown
                      aria-hidden="true"
                      className="mt-0.5 size-4 shrink-0 -rotate-90 transition-transform group-open:rotate-0"
                    />
                    <span className="min-w-0 space-y-1">
                      <span className="block text-sm font-medium wrap-anywhere">
                        {evidence.section || "ADR excerpt"}
                      </span>
                      <span className="block text-xs wrap-anywhere text-muted-foreground">
                        {source?.source_name ||
                          source?.title ||
                          evidence.adr_id}{" "}
                        · ADR: {evidence.adr_id}
                        {evidence.chunk_index !== null
                          ? ` · Chunk ${evidence.chunk_index + 1}`
                          : ""}
                      </span>
                    </span>
                  </summary>
                  <div className="space-y-3 border-t px-4 py-3">
                    <blockquote className="border-l-2 border-primary/30 pl-3 text-sm leading-7 wrap-anywhere whitespace-pre-wrap">
                      {evidence.excerpt}
                    </blockquote>
                    {evidence.truncated && (
                      <p className="text-xs text-muted-foreground">
                        Stored excerpt shortened to 1,200 characters.
                      </p>
                    )}
                    <p className="text-xs wrap-anywhere text-muted-foreground">
                      Chunk: {evidence.id}
                      {evidence.content_hash
                        ? ` · Hash: ${evidence.content_hash}`
                        : ""}
                    </p>
                  </div>
                </details>
              );
            })
          ) : (
            <p className="text-sm text-muted-foreground">
              No supporting chunks were persisted.
            </p>
          )}
          {detail.evidence_truncated && (
            <p className="text-xs text-muted-foreground">
              The response includes the first 50 supporting excerpts.
            </p>
          )}
        </section>
      </div>
      <div className="min-w-0 space-y-6 border-t pt-5 md:border-t-0 md:border-l md:pt-0 md:pl-6">
        <section
          aria-labelledby="knowledge-entities-heading"
          className="space-y-3"
        >
          <h3 id="knowledge-entities-heading" className="font-semibold">
            Related entities{" "}
            <span className="font-normal text-muted-foreground">
              ({entities.length})
            </span>
          </h3>
          <p className="text-xs leading-5 text-muted-foreground">
            Documented ADR assertions, rather than code-derived relationships or
            compliance results.
          </p>
          {entities.length ? (
            <ul className="divide-y rounded-xl border">
              {(allEntities ? entities : entities.slice(0, 8)).map((entity) => (
                <li
                  key={entity.id}
                  data-entity-id={entity.id}
                  className="space-y-2 px-3 py-2.5"
                >
                  <div className="min-w-0">
                    <p className="text-sm font-medium wrap-anywhere">
                      {entity.name || entity.id}
                    </p>
                    <p className="text-xs wrap-anywhere text-muted-foreground">
                      {entity.id}
                      {entity.labels.size
                        ? ` · ${Array.from(entity.labels).join(", ")}`
                        : ""}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {Array.from(entity.relationships).map((relationship) => (
                      <span
                        key={relationship}
                        className="rounded-full border bg-secondary/50 px-2 py-0.5 text-xs"
                      >
                        {relationship === "AFFECTS"
                          ? "Affected"
                          : "Constrained"}
                      </span>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">
              No related entities were persisted.
            </p>
          )}
          {entities.length > 8 && (
            <Button
              variant="outline"
              size="sm"
              aria-expanded={allEntities}
              onClick={() => setAllEntities(!allEntities)}
            >
              {allEntities
                ? "Show fewer entities"
                : `Show all ${entities.length} entities`}
            </Button>
          )}
          {detail.entities_truncated && (
            <p className="text-xs text-muted-foreground">
              Entity relationships are limited to the first 100 returned by the
              API.
            </p>
          )}
        </section>
        <section
          aria-labelledby="knowledge-provenance-heading"
          className="space-y-3 border-t pt-5"
        >
          <h3 id="knowledge-provenance-heading" className="font-semibold">
            Provenance
          </h3>
          {detail.sources.length ? (
            detail.sources.map((source) => (
              <details key={source.id} open className="group rounded-xl border">
                <summary className="flex cursor-pointer list-none items-start gap-2 rounded-xl p-3 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none [&::-webkit-details-marker]:hidden">
                  <ChevronDown
                    aria-hidden="true"
                    className="mt-0.5 size-4 shrink-0 -rotate-90 transition-transform group-open:rotate-0"
                  />
                  <span className="min-w-0">
                    <span className="block text-sm font-medium wrap-anywhere">
                      {source.title || source.source_name || source.id}
                    </span>
                    <span className="block text-xs wrap-anywhere text-muted-foreground">
                      {source.source_name || source.id}
                    </span>
                  </span>
                </summary>
                <dl className="space-y-2 border-t px-3 py-3 text-xs wrap-anywhere">
                  <div>
                    <dt className="text-muted-foreground">ADR ID</dt>
                    <dd>{source.id}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Source type</dt>
                    <dd>{source.source_type || "Unavailable"}</dd>
                  </div>
                  {source.source_version && (
                    <div>
                      <dt className="text-muted-foreground">Version</dt>
                      <dd>{source.source_version}</dd>
                    </div>
                  )}
                  {source.content_hash && (
                    <div>
                      <dt className="text-muted-foreground">Content hash</dt>
                      <dd className="font-mono">{source.content_hash}</dd>
                    </div>
                  )}
                </dl>
              </details>
            ))
          ) : (
            <p className="text-sm text-muted-foreground">
              No source provenance was persisted.
            </p>
          )}
        </section>
      </div>
    </div>
  );
}
