import { ContentLoading } from "@/components/core/query-feedback";
import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChatPanel } from "@/components/explore/chat-panel";
import { GraphPanel } from "@/components/explore/graph-panel";
import { Button } from "@/components/ui/button";
import { exploreService } from "@/api/explore-service";
import { useAuth } from "@/contexts/auth-context";
import { useIsMobile } from "@/hooks/use-mobile";
import { useRepositoryOverview } from "@/layouts/repository-layout";
import { getApiErrorMessage } from "@/lib/api-error";
import { clampSplit, restoreSplit, SPLIT_KEY } from "@/lib/explorer-model";
import type { AnswerContext } from "@/types/explore";

export default function ExplorePage() {
  const { repository } = useRepositoryOverview();
  const { user, token } = useAuth();
  if (!user || !token) return null;
  return (
    <ScopedExplorer
      key={`${user.id}:${token}:${repository.id}`}
      repositoryId={repository.id}
      userId={user.id}
      token={token}
    />
  );
}
function ScopedExplorer({
  repositoryId,
  userId,
  token,
}: {
  repositoryId: string;
  userId: string;
  token: string;
}) {
  const isMobile = useIsMobile();
  const [split, setSplit] = useState(restoreSplit);
  const [tab, setTab] = useState<"chat" | "graph">("chat");
  const [context, setContext] = useState<AnswerContext | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const graph = useQuery({
    enabled: !isMobile || tab === "graph",
    queryKey: ["repository-graph", userId, token, repositoryId],
    queryFn: ({ signal }) => exploreService.graph(repositoryId, signal),
    retry: false,
    staleTime: 5 * 60_000,
    gcTime: 15 * 60_000,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const saveSplit = (value: number) => {
    const next = clampSplit(value);
    setSplit(next);
    try {
      localStorage.setItem(SPLIT_KEY, String(next));
    } catch {
      /* Layout works without storage. */
    }
  };
  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">
          Knowledge Explorer
        </h1>
        <Button size="sm" variant="outline" onClick={() => saveSplit(40)}>
          Reset Layout
        </Button>
      </div>
      <div
        role="tablist"
        aria-label="Explorer panels"
        className="flex shrink-0 gap-2 md:hidden"
      >
        {(["chat", "graph"] as const).map((value) => (
          <Button
            key={value}
            id={`explorer-${value}-tab`}
            role="tab"
            aria-selected={tab === value}
            aria-controls={`explorer-${value}-panel`}
            variant={tab === value ? "secondary" : "ghost"}
            onClick={() => setTab(value)}
          >
            {value === "chat" ? "Chat" : "Graph"}
          </Button>
        ))}
      </div>
      <div
        ref={container}
        data-slot="explorer-split"
        className="flex min-h-0 w-full flex-1 overflow-hidden"
        style={
          {
            "--chat-width": `calc(${split}% - ${(12 * split) / 100}px)`,
          } as React.CSSProperties
        }
      >
        <div
          id="explorer-chat-panel"
          className={`${tab === "chat" ? "block" : "hidden"} min-h-0 w-full min-w-0 md:block md:w-[var(--chat-width)]`}
        >
          <ChatPanel
            repositoryId={repositoryId}
            userId={userId}
            token={token}
            onConversationChange={() => setContext(null)}
            onContext={(answer) => {
              setContext(answer);
              setTab("graph");
            }}
          />
        </div>
        <div
          role="separator"
          aria-label="Resize chat and graph panels"
          aria-orientation="vertical"
          aria-valuemin={25}
          aria-valuemax={75}
          aria-valuenow={Math.round(split)}
          aria-controls="explorer-chat-panel explorer-graph-panel"
          tabIndex={0}
          className="group hidden w-3 shrink-0 cursor-col-resize touch-none items-center justify-center outline-none focus-visible:bg-primary/15 md:flex"
          onPointerDown={(event) => {
            event.preventDefault();
            event.currentTarget.focus();
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            if (
              !event.currentTarget.hasPointerCapture(event.pointerId) ||
              !container.current
            )
              return;
            const bounds = container.current.getBoundingClientRect();
            saveSplit(
              ((event.clientX - bounds.left) / (bounds.width - 12)) * 100
            );
          }}
          onPointerUp={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId))
              event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
              return;
            event.preventDefault();
            saveSplit(
              event.key === "Home"
                ? 25
                : event.key === "End"
                  ? 75
                  : split + (event.key === "ArrowRight" ? 2 : -2)
            );
          }}
        >
          <span className="h-12 w-1 rounded-full bg-border group-hover:bg-primary group-focus-visible:bg-primary" />
        </div>
        <div
          id="explorer-graph-panel"
          className={`${tab === "graph" ? "block" : "hidden"} min-h-0 min-w-0 flex-1 md:block`}
        >
          {graph.isError ? (
            <section
              role="alert"
              className="h-full space-y-3 rounded-xl border p-5"
            >
              <h2 className="font-semibold">Graph unavailable</h2>
              <p className="text-sm">
                {getApiErrorMessage(
                  graph.error,
                  "Repository graph could not be loaded."
                )}
              </p>
              <Button variant="outline" onClick={() => void graph.refetch()}>
                Retry graph
              </Button>
            </section>
          ) : graph.isPending ? (
            <ContentLoading label="Loading repository graph..." />
          ) : !graph.data.nodes.length ? (
            <section className="h-full space-y-3 rounded-xl border p-5">
              <h2 className="font-semibold">No graph entities yet</h2>
              <p className="text-sm text-muted-foreground">
                The repository has no persisted entity graph.
              </p>
              {context && (
                <p role="status" className="text-sm">
                  Answer Context has no available graph matches.
                </p>
              )}
              {context && (
                <Button variant="outline" onClick={() => setContext(null)}>
                  Clear Highlight
                </Button>
              )}
            </section>
          ) : (
            <GraphPanel
              graph={graph.data}
              context={context}
              clearContext={() => setContext(null)}
              visible={tab === "graph" || !isMobile}
            />
          )}
        </div>
      </div>
    </div>
  );
}
