from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GraphContext:
    """Frontend-oriented graph highlighting metadata for a RAG retrieval result.

    Attributes:
        node_ids: Unique, deterministically sorted canonical entity node IDs (e.g. 'entity:OrderService').
        edge_ids: Unique, deterministically sorted architectural relationship edge IDs (e.g. 'edge:OrderService:CALLS:PaymentGateway').
        assertion_ids: Unique, deterministically sorted GraphAssertion provenance IDs (e.g. 'assertion:adr:...').
    """

    node_ids: list[str] = field(default_factory=list)
    edge_ids: list[str] = field(default_factory=list)
    assertion_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "node_ids": list(self.node_ids),
            "edge_ids": list(self.edge_ids),
            "assertion_ids": list(self.assertion_ids),
        }
