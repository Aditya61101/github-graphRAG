import type {
  ConversationHistory,
  ExploreChatMessage,
  GraphContext,
  QueryResponse,
  RepositoryGraph,
} from "@/types/explore";

export const emptyContext = (): GraphContext => ({
  node_ids: [],
  edge_ids: [],
  assertion_ids: [],
});

// Keep the backend contract at this boundary; each message retains its own
// evidence and context, including assertions the graph API does not render.
export function adaptQueryResponse(
  response: QueryResponse
): ExploreChatMessage {
  return {
    id: crypto.randomUUID(),
    role: "assistant",
    content: response.answer,
    sources: response.sources ?? [],
    graphContext: {
      node_ids: response.graph_context?.node_ids ?? [],
      edge_ids: response.graph_context?.edge_ids ?? [],
      assertion_ids: response.graph_context?.assertion_ids ?? [],
    },
  };
}
export function createConversationHistory(): ConversationHistory {
  const id = crypto.randomUUID();
  return {
    activeId: id,
    conversations: [{ id, title: "New conversation", messages: [] }],
  };
}
export function matchGraphContext(
  graph: RepositoryGraph,
  context: GraphContext
) {
  const nodes = new Set(graph.nodes.map((node) => node.id));
  const edges = new Map(graph.edges.map((edge) => [edge.id, edge]));
  const matchedNodes = new Set(context.node_ids.filter((id) => nodes.has(id)));
  const matchedEdges = new Set(context.edge_ids.filter((id) => edges.has(id)));
  const fitNodes = new Set(matchedNodes);
  // Include endpoints for fitting explicitly referenced edges. Do not infer
  // other relationships just because they connect the same nodes.
  for (const id of matchedEdges) {
    const edge = edges.get(id)!;
    if (nodes.has(edge.source)) fitNodes.add(edge.source);
    if (nodes.has(edge.target)) fitNodes.add(edge.target);
  }
  return {
    nodes: matchedNodes,
    edges: matchedEdges,
    fitNodes,
    missing: new Set([
      ...context.node_ids.filter((id) => !nodes.has(id)),
      ...context.edge_ids.filter((id) => !edges.has(id)),
    ]).size,
  };
}
export const SPLIT_KEY = "decisionguard:explorer:split";
export const clampSplit = (value: number) => Math.max(25, Math.min(75, value));
export function restoreSplit(): number {
  try {
    const stored = localStorage.getItem(SPLIT_KEY);
    const value = stored === null ? 40 : Number(stored);
    return Number.isFinite(value) ? clampSplit(value) : 40;
  } catch {
    return 40;
  }
}
