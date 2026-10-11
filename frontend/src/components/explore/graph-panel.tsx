import { useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  BaseEdge,
  EdgeLabelRenderer,
  MarkerType,
  Position,
  useReactFlow,
  useNodesInitialized,
  useStore,
  type Node,
  type Edge,
  type EdgeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/contexts/theme-context";
import { matchGraphContext } from "@/lib/explorer-model";
import {
  prepareGraphLayout,
  routedEdgePath,
  type GraphRoute,
} from "@/lib/graph-layout";
import type {
  AnswerContext,
  GraphNode,
  GraphEdge,
  RepositoryGraph,
} from "@/types/explore";

type Selection = { kind: "node" | "edge"; id: string } | null;
type RelationshipData = {
  type: string;
  offset: number;
  highlighted: boolean;
  route?: GraphRoute;
  select: () => void;
};
type Relationship = Edge<RelationshipData, "relationship">;
const edgeTypes = { relationship: RelationshipEdge };

function RelationshipEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  style,
  data,
}: EdgeProps<Relationship>) {
  const offset = data?.offset ?? 0;
  const bend = Math.max(60, Math.abs(targetX - sourceX) / 2);
  const path = data?.route
    ? routedEdgePath(data.route.points)
    : `M ${sourceX},${sourceY} C ${sourceX + bend},${sourceY + offset} ${targetX - bend},${targetY + offset} ${targetX},${targetY}`;
  const label = data?.route?.label ?? {
    x: (sourceX + targetX) / 2,
    y: (sourceY + targetY) / 2 + offset * 0.75,
  };
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={style}
        interactionWidth={24}
      />
      <EdgeLabelRenderer>
        <button
          type="button"
          tabIndex={-1}
          onClick={data?.select}
          className="nodrag nopan absolute max-w-[240px] truncate rounded border bg-card px-1.5 py-0.5 text-[10px] text-card-foreground"
          title={data?.type}
          style={{
            pointerEvents: "all",
            transform: `translate(-50%, -50%) translate(${label.x}px,${label.y}px)`,
            opacity: style?.opacity,
          }}
          aria-label={`Inspect ${data?.type} relationship`}
        >
          {data?.type}
        </button>
      </EdgeLabelRenderer>
    </>
  );
}

export function GraphPanel(props: {
  graph: RepositoryGraph;
  context: AnswerContext | null;
  clearContext: () => void;
  visible: boolean;
}) {
  return (
    <ReactFlowProvider>
      {props.visible && <GraphDisplay {...props} />}
    </ReactFlowProvider>
  );
}

function GraphDisplay({
  graph: sourceGraph,
  context,
  clearContext,
  visible,
}: {
  graph: RepositoryGraph;
  context: AnswerContext | null;
  clearContext: () => void;
  visible: boolean;
}) {
  const [selection, setSelection] = useState<Selection>(null);
  const { theme } = useTheme();
  const flow = useReactFlow();
  const initialized = useNodesInitialized();
  const canvasWidth = useStore((state) => state.width);
  const canvasHeight = useStore((state) => state.height);
  const prepared = useMemo(
    () => prepareGraphLayout(sourceGraph),
    [sourceGraph]
  );
  const { graph, positions } = prepared;
  const matches = useMemo(
    () => (context ? matchGraphContext(graph, context.context) : null),
    [graph, context]
  );
  const nodes: Node[] = useMemo(
    () =>
      graph.nodes.map((node) => ({
        id: node.id,
        position: positions.get(node.id)!,
        data: {
          label: (
            <div className="space-y-1">
              <span
                className="block truncate text-sm font-medium"
                title={node.label}
              >
                {node.label}
              </span>
              <span className="block text-xs text-muted-foreground">
                {node.type}
              </span>
            </div>
          ),
        },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        selected: selection?.kind === "node" && selection.id === node.id,
        style: {
          width: 190,
          height: 72,
          background: "var(--card)",
          color: "var(--card-foreground)",
          border: `2px solid ${matches?.nodes.has(node.id) ? "var(--primary)" : "var(--border)"}`,
          borderRadius: 12,
          opacity: matches && !matches.nodes.has(node.id) ? 0.3 : 1,
        },
        className: matches?.nodes.has(node.id) ? "answer-context-node" : "",
      })),
    [graph, positions, matches, selection]
  );
  const edges: Relationship[] = useMemo(() => {
    const parallel = new Map<string, GraphEdge[]>();
    for (const edge of graph.edges) {
      const key = [edge.source, edge.target].sort().join("\n");
      parallel.set(key, [...(parallel.get(key) ?? []), edge]);
    }
    return graph.edges.map((edge) => {
      const group = parallel.get([edge.source, edge.target].sort().join("\n"))!;
      const selected = selection?.kind === "edge" && selection.id === edge.id;
      const highlighted = matches?.edges.has(edge.id) ?? false;
      const color =
        highlighted || selected ? "var(--primary)" : "var(--muted-foreground)";
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: "relationship",
        selected,
        markerEnd: { type: MarkerType.ArrowClosed, color },
        data: {
          type: edge.type,
          offset: (group.indexOf(edge) - (group.length - 1) / 2) * 60,
          highlighted,
          route: prepared.routes.get(edge.id),
          select: () => setSelection({ kind: "edge", id: edge.id }),
        },
        style: {
          stroke: color,
          strokeWidth: highlighted || selected ? 3 : 1.5,
          opacity: matches && !highlighted ? 0.2 : 1,
        },
        className: highlighted ? "answer-context-edge" : "",
        ariaLabel: `${edge.type}: ${edge.source} to ${edge.target}`,
      };
    });
  }, [graph, matches, selection, prepared.routes]);
  useEffect(() => {
    if (!initialized || !visible || !canvasWidth || !canvasHeight) return;
    const frame = requestAnimationFrame(
      () =>
        void flow.fitView({
          nodes: matches?.fitNodes.size
            ? Array.from(matches.fitNodes, (id) => ({ id }))
            : undefined,
          padding: 0.25,
          duration: 250,
          maxZoom: 1.25,
        })
    );
    return () => cancelAnimationFrame(frame);
  }, [initialized, visible, context, matches, flow, canvasWidth, canvasHeight]);
  const selected =
    selection?.kind === "node"
      ? graph.nodes.find((node) => node.id === selection.id)
      : graph.edges.find((edge) => edge.id === selection?.id);
  return (
    <section
      aria-label="Entity Relationship Graph"
      className="relative flex h-full min-h-0 flex-col overflow-hidden rounded-xl border bg-card"
    >
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b p-3">
        <h2 className="font-semibold">Entity Relationship Graph</h2>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => void flow.fitView({ padding: 0.2 })}
          >
            Fit graph
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              clearContext();
              setSelection(null);
              void flow.fitView({ padding: 0.2 });
            }}
          >
            Reset graph
          </Button>
        </div>
      </header>
      {(prepared.unavailableEdges > 0 || prepared.fallback) && (
        <p
          role="status"
          title={prepared.failureReason ?? undefined}
          className="shrink-0 border-b px-3 py-2 text-xs text-muted-foreground"
        >
          {prepared.unavailableEdges > 0 &&
            `${prepared.unavailableEdges} relationships have unavailable endpoints and cannot be displayed. `}
          {prepared.fallback &&
            "Automatic layout unavailable; using a simple layout."}
        </p>
      )}
      {context && (
        <div
          role="status"
          className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b bg-muted/40 px-3 py-2 text-xs"
        >
          <span>
            Answer Context · {matches?.nodes.size ?? 0} nodes ·{" "}
            {matches?.edges.size ?? 0} edges
            {matches?.missing
              ? ` · ${matches.missing} unavailable identifiers`
              : ""}
            {!matches?.fitNodes.size ? " · No matching graph elements" : ""}
          </span>
          <Button size="sm" variant="ghost" onClick={clearContext}>
            Clear Highlight
          </Button>
        </div>
      )}
      <div className="min-h-0 flex-1" data-slot="graph-canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          edgeTypes={edgeTypes}
          fitView
          minZoom={0.1}
          maxZoom={2.5}
          nodesDraggable={false}
          nodesConnectable={false}
          colorMode={theme}
          onNodesChange={(changes) => {
            const selected = changes.find(
              (change) => change.type === "select" && change.selected
            );
            if (selected?.type === "select")
              setSelection({ kind: "node", id: selected.id });
          }}
          onEdgesChange={(changes) => {
            const selected = changes.find(
              (change) => change.type === "select" && change.selected
            );
            if (selected?.type === "select")
              setSelection({ kind: "edge", id: selected.id });
          }}
          onNodeClick={(_, node) => setSelection({ kind: "node", id: node.id })}
          onEdgeClick={(_, edge) => setSelection({ kind: "edge", id: edge.id })}
          onPaneClick={() => setSelection(null)}
          style={{ background: "var(--background)" }}
        >
          <Background color="var(--border)" />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      {selected && (
        <GraphInspector
          item={selected}
          kind={selection!.kind}
          close={() => setSelection(null)}
        />
      )}
    </section>
  );
}

function GraphInspector({
  item,
  kind,
  close,
}: {
  item: GraphNode | GraphEdge;
  kind: "node" | "edge";
  close: () => void;
}) {
  return (
    <aside
      aria-label="Graph details"
      className="absolute right-3 bottom-3 z-10 max-h-[45%] w-[min(320px,calc(100%-1.5rem))] overflow-y-auto rounded-xl border bg-popover p-3 text-popover-foreground shadow-lg"
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold">
          {kind === "node" ? (item as GraphNode).label : item.type}
        </h3>
        <Button size="sm" variant="ghost" onClick={close}>
          Close details
        </Button>
      </div>
      <dl className="space-y-2 text-xs wrap-anywhere">
        <div>
          <dt className="text-muted-foreground">ID</dt>
          <dd>{item.id}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Type</dt>
          <dd>{item.type}</dd>
        </div>
        {kind === "edge" && (
          <>
            <div>
              <dt className="text-muted-foreground">From</dt>
              <dd>{(item as GraphEdge).source}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">To</dt>
              <dd>{(item as GraphEdge).target}</dd>
            </div>
          </>
        )}
        {Object.entries(item.metadata).map(([key, value]) => (
          <div key={key}>
            <dt className="text-muted-foreground">
              {key.replaceAll("_", " ")}
            </dt>
            <dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}
