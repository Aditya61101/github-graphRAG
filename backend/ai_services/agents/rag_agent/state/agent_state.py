from __future__ import annotations

from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentState
from langgraph.channels import UntrackedValue


class RAGAgentState(AgentState):
    """Agent state with per-run retrieval metadata.

    ``retrieval_sources`` is intentionally untracked so it is available in the
    current invocation result but is not persisted by the checkpointer as part
    of conversation memory.
    """

    retrieval_sources: NotRequired[
        Annotated[dict[str, list[dict]], UntrackedValue]
    ]
    repository_id: NotRequired[str | None]
