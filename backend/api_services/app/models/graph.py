from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    """Canonical architectural entity node formatted for frontend graph rendering."""

    id: str = Field(description="Stable application-level node ID, e.g. 'entity:<canonical_id>'")
    label: str = Field(description="Human-readable entity name, e.g. 'OrderService'")
    type: str = Field(description="Architectural entity type/label, e.g. 'Service', 'Component', 'Database'")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional lightweight entity metadata")


class GraphEdge(BaseModel):
    """Architectural relationship edge formatted for frontend graph rendering."""

    id: str = Field(description="Stable application-level edge ID, e.g. 'edge:<src>:<type>:<tgt>'")
    source: str = Field(description="Source node ID referencing GraphNode.id, e.g. 'entity:<source_id>'")
    target: str = Field(description="Target node ID referencing GraphNode.id, e.g. 'entity:<target_id>'")
    type: str = Field(description="Architectural relationship type, e.g. 'DEPENDS_ON', 'CALLS', 'USES'")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional lightweight edge metadata")


class RepositoryGraphResponse(BaseModel):
    """Full repository architectural graph response containing canonical nodes and edges."""

    nodes: list[GraphNode] = Field(default_factory=list, description="Canonical architectural entities")
    edges: list[GraphEdge] = Field(default_factory=list, description="Architectural relationships between entities")
