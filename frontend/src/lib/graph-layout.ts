import dagre from "@dagrejs/dagre";
import type { RepositoryGraph } from "@/types/explore";

export type GraphPoint = { x: number; y: number };
export type GraphRoute = { points: GraphPoint[]; label: GraphPoint };

export function routedEdgePath(points: GraphPoint[]) {
  if (points.length < 2) return "";
  let path = `M ${points[0].x},${points[0].y}`;
  for (let index = 1; index < points.length - 1; index++) {
    const previous = points[index - 1],
      point = points[index],
      next = points[index + 1];
    const before = Math.hypot(point.x - previous.x, point.y - previous.y);
    const after = Math.hypot(next.x - point.x, next.y - point.y);
    if (!before || !after) continue;
    const radius = Math.min(12, before / 2, after / 2);
    const entry = {
      x: point.x + ((previous.x - point.x) * radius) / before,
      y: point.y + ((previous.y - point.y) * radius) / before,
    };
    const exit = {
      x: point.x + ((next.x - point.x) * radius) / after,
      y: point.y + ((next.y - point.y) * radius) / after,
    };
    path += ` L ${entry.x},${entry.y} Q ${point.x},${point.y} ${exit.x},${exit.y}`;
  }
  const last = points[points.length - 1];
  return `${path} L ${last.x},${last.y}`;
}

export function prepareGraphLayout(graph: RepositoryGraph) {
  const ids = new Set(graph.nodes.map((node) => node.id));
  // Dagre creates dimensionless implicit nodes for absent endpoints, which can
  // throw during rectangle intersection. Such edges cannot be drawn by React Flow.
  const edges = graph.edges.filter(
    (edge) => ids.has(edge.source) && ids.has(edge.target)
  );
  // Dagre is pinned to 1.1.8: 3.1.1 throws during boundary intersection for
  // valid cyclic multigraphs. Keep the parallel/reverse-edge regression tests
  // when upgrading; do not simplify or discard valid API relationships.
  const layout = new dagre.graphlib.Graph({ multigraph: true });
  layout.setDefaultEdgeLabel(() => ({}));
  layout.setGraph({
    rankdir: "LR",
    nodesep: 80,
    edgesep: 48,
    ranksep: 180,
    marginx: 30,
    marginy: 30,
  });
  for (const node of graph.nodes)
    layout.setNode(node.id, { width: 190, height: 72 });
  for (const edge of edges)
    layout.setEdge(
      edge.source,
      edge.target,
      {
        width: Math.min(240, Math.max(70, (edge.type?.length ?? 0) * 7 + 20)),
        height: 28,
        labelpos: "c",
      },
      edge.id
    );
  let fallback = false;
  let failureReason: string | null = null;
  const routes = new Map<string, GraphRoute>();
  let positions: Map<string, { x: number; y: number }>;
  try {
    dagre.layout(layout);
    positions = new Map(
      graph.nodes.map((node) => {
        const point = layout.node(node.id);
        if (!Number.isFinite(point?.x) || !Number.isFinite(point?.y))
          throw new Error("Graph layout returned invalid coordinates");
        return [node.id, { x: point.x - 95, y: point.y - 36 }];
      })
    );
    for (const edge of edges) {
      const route = layout.edge(edge.source, edge.target, edge.id);
      if (
        !route.points?.length ||
        !route.points.every(
          (point: GraphPoint) =>
            Number.isFinite(point.x) && Number.isFinite(point.y)
        )
      )
        throw new Error("Graph layout returned invalid edge routes");
      routes.set(edge.id, {
        points: route.points,
        label: { x: route.x, y: route.y },
      });
    }
  } catch (error) {
    // Keep graph exploration and chat usable even for unsupported topologies.
    fallback = true;
    failureReason =
      error instanceof Error ? error.message : "Unknown graph layout failure";
    routes.clear();
    const columns = Math.max(1, Math.ceil(Math.sqrt(graph.nodes.length)));
    positions = new Map(
      graph.nodes.map((node, index) => [
        node.id,
        {
          x: 30 + (index % columns) * 300,
          y: 30 + Math.floor(index / columns) * 150,
        },
      ])
    );
  }
  return {
    graph: { ...graph, edges },
    positions,
    routes,
    fallback,
    failureReason,
    unavailableEdges: graph.edges.length - edges.length,
  };
}
