import type { Project } from "@/types/project";

export const MOCK_PROJECTS: Project[] = [
  {
    id: "proj-001",
    name: "GraphRAG Explorer",
    description:
      "Interactive knowledge graph explorer for repository-aware retrieval augmented generation.",
    repositoryId: "repo-001",
    repositoryName: "graph-rag-explorer",
    updatedAt: "2026-10-05T14:32:00Z",
  },
  {
    id: "proj-002",
    name: "Issue Pulse AI",
    description:
      "AI-powered GitHub issue triage dashboard with sentiment and priority analysis.",
    repositoryId: "repo-002",
    repositoryName: "issue-pulse-ai",
    updatedAt: "2026-10-06T09:10:00Z",
  },
  {
    id: "proj-003",
    name: "Code Atlas",
    description:
      "Repository visualization toolkit for mapping files, commits, and contributors.",
    repositoryId: "repo-003",
    repositoryName: "code-atlas",
    updatedAt: "2026-10-07T05:48:00Z",
  },
  {
    id: "proj-004",
    name: "PR Review Copilot",
    description:
      "Automated pull request review assistant with contextual code suggestions.",
    repositoryId: "repo-004",
    repositoryName: "pr-review-copilot",
    updatedAt: "2026-10-07T11:22:00Z",
  },
  {
    id: "proj-005",
    name: "Semantic Repo Search",
    description:
      "Semantic search engine for large repositories using embeddings and vector indexing.",
    repositoryId: "repo-005",
    repositoryName: "semantic-repo-search",
    updatedAt: "2026-10-07T16:05:00Z",
  },
];
