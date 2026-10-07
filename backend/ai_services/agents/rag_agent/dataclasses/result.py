from __future__ import annotations

from dataclasses import dataclass, field

from ai_services.graph.context import GraphContext


@dataclass(frozen=True)
class RAGAgentResult:
    answer: str
    sources: dict[str, list[dict]]
    graph_context: GraphContext = field(default_factory=GraphContext)
