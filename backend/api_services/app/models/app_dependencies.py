from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from neo4j import Driver

from shared.ai.provider import Embedder
from shared.ai.provider import LLM
from ai_services.retrievers.reranker import Reranker
from ai_services.retrievers.settings import RetrievalSettings


@dataclass
class AppDependencies:
    """Application-scoped infrastructure shared by services and agents."""

    driver: Driver
    client: Any
    embedder: Embedder
    llm: LLM
    chat_llm: BaseChatModel
    entity_retriever: Any
    chunk_retriever: Any
    community_retriever: Any
    reranker: Reranker | None
    retrieval_settings: RetrievalSettings
    ingestion_service: Any = None
    pr_ingestion_service: Any = None
    repo_store: Any = None
    sqlite_store: Any = None
    adr_service: Any = None
    graph_service: Any = None
