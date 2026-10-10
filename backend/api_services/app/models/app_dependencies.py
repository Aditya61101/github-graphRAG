from dataclasses import dataclass
from typing import Any

from langchain_openai import AzureChatOpenAI
from neo4j import Driver
from openai import AsyncAzureOpenAI

from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from shared.utils.llm_utils import AzureOpenAILLM
from ai_services.retrievers.reranker import Reranker
from ai_services.retrievers.settings import RetrievalSettings


@dataclass
class AppDependencies:
    """Application-scoped infrastructure shared by services and agents."""

    driver: Driver
    client: AsyncAzureOpenAI
    embedder: AzureOpenAIEmbedder
    llm: AzureOpenAILLM
    chat_llm: AzureChatOpenAI
    entity_retriever: Any
    chunk_retriever: Any
    community_retriever: Any
    reranker: Reranker
    retrieval_settings: RetrievalSettings
    ingestion_service: Any = None
    pr_ingestion_service: Any = None
    repo_store: Any = None
    sqlite_store: Any = None
    adr_service: Any = None
    graph_service: Any = None
