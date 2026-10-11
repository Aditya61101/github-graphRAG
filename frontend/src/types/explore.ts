export type GraphContext = {
  node_ids: string[];
  edge_ids: string[];
  assertion_ids: string[];
};
export type QuerySource = {
  file_path: string;
  excerpt: string;
  score: number | null;
  score_type: string | null;
};
export type QueryResponse = {
  answer: string;
  conversation_id: string;
  sources: QuerySource[];
  graph_context: GraphContext;
};
export type ExploreChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources: QuerySource[];
  graphContext: GraphContext;
};
export type Conversation = {
  id: string;
  title: string;
  messages: ExploreChatMessage[];
};
export type ConversationHistory = {
  activeId: string;
  conversations: Conversation[];
};
export type GraphNode = {
  id: string;
  label: string;
  type: string;
  metadata: Record<string, unknown>;
};
export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  type: string;
  metadata: Record<string, unknown>;
};
export type RepositoryGraph = { nodes: GraphNode[]; edges: GraphEdge[] };
export type AnswerContext = { messageId: string; context: GraphContext };
