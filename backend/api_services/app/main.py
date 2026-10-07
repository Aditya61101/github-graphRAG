from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_openai import AzureChatOpenAI
from neo4j import GraphDatabase
from openai import AsyncAzureOpenAI
from starlette.middleware.sessions import SessionMiddleware

import os

from api_services.app.routers.auth import router as auth_router
from api_services.app.routers.query import router as query_router
from api_services.app.routers.health import router as health_router
from api_services.app.routers.webhook import router as webhook_router
from api_services.app.routers.repositories import router as repositories_router

from api_services.app.config import (
    ADRS_STORAGE_DIR,
    FRONTEND_URL,
    MAX_ADR_FILE_SIZE_BYTES,
    REPOS_STORAGE_DIR,
    RETRIEVAL_SETTINGS,
)
from ai_services.agents.rag_agent.agent import RAGQueryAgent
from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from ai_services.ingestion.adr.chunker import ADRChunker
from ai_services.ingestion.adr.extractor import ADRArchitecturalExtractor
from ai_services.ingestion.adr.neo4j_writer import ADRNeo4jWriter
from ai_services.ingestion.adr.processor import ADRProcessingService
from ai_services.ingestion.adr.resolver import ADREntityResolver
from ai_services.ingestion.adr.service import ADRService
from ai_services.ingestion.persistence.sqlite_store import (
    SqliteApplicationStore,
    SqliteCredentialProvider,
)
from ai_services.ingestion.service import RepositoryIngestionService
from ai_services.ingestion.sources.github import GitHubRepositorySource
from api_services.app.models.app_dependencies import AppDependencies
from ai_services.retrievers.retriever_factory import create_retrievers
from ai_services.retrievers.reranker import Qwen3Reranker
from ai_services.graph import Neo4jGraphRepository, RepositoryGraphService
from shared.utils.env_helper import require_env
from shared.utils.llm_utils import AzureOpenAILLM


@asynccontextmanager
async def lifespan(app: FastAPI):
    database = require_env("NEO4J_DATABASE") or None
    # One model per FastAPI worker; failures propagate and fail startup.
    reranker = await asyncio.to_thread(Qwen3Reranker, RETRIEVAL_SETTINGS)
    app.state.reranker = reranker

    # Create application-scoped infrastructure once. These instances are
    # reused by ingestion, retrieval, and agents for the lifetime of this
    # FastAPI worker process.
    driver = GraphDatabase.driver(
        require_env("NEO4J_URI"),
        auth=(
            require_env("NEO4J_USERNAME"),
            require_env("NEO4J_PASSWORD"),
        ),
    )

    client = AsyncAzureOpenAI(
        azure_endpoint=require_env("AZURE_OPENAI_ENDPOINT"),
        api_key=require_env("AZURE_OPENAI_API_KEY"),
        api_version=require_env("AZURE_OPENAI_API_VERSION"),
    )

    embedder = AzureOpenAIEmbedder(
        client=client,
        deployment=require_env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"),
        dimensions=int(require_env("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072")),
    )

    # LangChain chat model for create_agent/SummarizationMiddleware.
    chat_llm = AzureChatOpenAI(
        azure_endpoint=require_env("AZURE_OPENAI_ENDPOINT"),
        api_key=require_env("AZURE_OPENAI_API_KEY"),
        api_version=require_env("AZURE_OPENAI_API_VERSION"),
        azure_deployment=require_env("AZURE_OPENAI_DEPLOYMENT_NAME"),
    )

    # Keep the existing SDK-backed LLM for components that use the raw
    # AsyncAzureOpenAI client/wrapper.
    llm = AzureOpenAILLM(
        client=client,
        deployment=require_env("AZURE_OPENAI_DEPLOYMENT_NAME"),
    )

    entity_retriever, community_retriever, chunk_retriever = create_retrievers(
        driver=driver,
        database=database,
    )

    # Initialize SQLite relational application store and credential provider
    sqlite_store = SqliteApplicationStore()
    cred_provider = SqliteCredentialProvider(sqlite_store)

    # Initialize GitHub repository source and ingestion service
    repo_source = GitHubRepositorySource(
        storage_dir=REPOS_STORAGE_DIR,
        credential_provider=cred_provider,
    )

    groq_api_key = os.getenv("GROQ_API_KEY")
    groq_client = None
    if groq_api_key:
        try:
            from groq import Groq
            groq_client = Groq(api_key=groq_api_key)
        except Exception:
            pass

    ingestion_service = RepositoryIngestionService(
        repository_source=repo_source,
        metadata_store=sqlite_store,
        neo4j_driver=driver,
        database=database,
        embedder=embedder,
        llm=llm,
        groq_client=groq_client,
    )

    if embedder is None:
        raise RuntimeError("ADR Phase 2 requires the configured embedding service")

    adr_writer = ADRNeo4jWriter(
        driver=driver,
        database=database or "neo4j",
        embedding_dimensions=embedder.dimensions if embedder else 3072,
    )
    adr_writer.initialize_constraints()

    adr_processor = ADRProcessingService(
        chunker=ADRChunker(),
        extractor=ADRArchitecturalExtractor(llm=llm),
        resolver=ADREntityResolver(
            driver=driver,
            database=database or "neo4j",
            embedder=embedder,
            embedding_dimensions=embedder.dimensions if embedder else 3072,
        ),
        writer=adr_writer,
        embedder=embedder,
        embedding_dimensions=embedder.dimensions if embedder else 3072,
    )

    adr_service = ADRService(
        sqlite_store=sqlite_store,
        storage_dir=ADRS_STORAGE_DIR,
        max_file_size_bytes=MAX_ADR_FILE_SIZE_BYTES,
        processor=adr_processor,
    )

    graph_repo = Neo4jGraphRepository(driver=driver, database=database or "neo4j")
    graph_service = RepositoryGraphService(sqlite_store=sqlite_store, graph_repo=graph_repo)

    app.state.sqlite_store = sqlite_store
    app.state.ingestion_service = ingestion_service
    app.state.adr_service = adr_service
    app.state.graph_service = graph_service

    app.state.deps = AppDependencies(
        driver=driver,
        client=client,
        embedder=embedder,
        llm=llm,
        chat_llm=chat_llm,
        entity_retriever=entity_retriever,
        chunk_retriever=chunk_retriever,
        community_retriever=community_retriever,
        reranker=reranker,
        retrieval_settings=RETRIEVAL_SETTINGS,
        ingestion_service=ingestion_service,
        sqlite_store=sqlite_store,
        adr_service=adr_service,
        graph_service=graph_service,
    )

    # Create the conversational agent once. Creating it inside the request
    # handler would create a new InMemorySaver and lose conversation history.
    app.state.rag_agent = RAGQueryAgent(
        driver=driver,
        database=database,
        embedder=embedder,
        entity_retriever=entity_retriever,
        community_retriever=community_retriever,
        chunk_retriever=chunk_retriever,
        llm=chat_llm,
        reranker=reranker,
        retrieval_settings=RETRIEVAL_SETTINGS,
    )

    try:
        yield
    finally:
        driver.close()


app = FastAPI(
    lifespan=lifespan,
    title="DecisionGuard GraphRAG API",
    version="0.1.0",
)

app.add_middleware(
    SessionMiddleware,
    secret_key="super-secret-session-key",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/auth")
app.include_router(query_router, prefix="/query")
app.include_router(health_router, prefix="/health")
app.include_router(webhook_router, prefix="/webhooks")
app.include_router(repositories_router, prefix="/repositories")
