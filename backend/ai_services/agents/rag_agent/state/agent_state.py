from __future__ import annotations

from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentState
from langgraph.channels import UntrackedValue


class RAGAgentState(AgentState):
    """Agent state with per-run retrieval metadata.

    ``retrieval_sources`` and ``retrieval_graph_context`` are intentionally untracked
    so they are available in the current invocation result but are not persisted
    by the checkpointer as part of conversation memory.
    """

    retrieval_sources: NotRequired[
        Annotated[dict[str, list[dict]], UntrackedValue]
    ]
    retrieval_graph_context: NotRequired[
        Annotated[dict[str, list[str]], UntrackedValue]
    ]
    repository_id: NotRequired[str | None]
