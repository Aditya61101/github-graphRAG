from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_openai import AzureChatOpenAI
from neo4j import GraphDatabase
from openai import AsyncAzureOpenAI
from starlette.middleware.sessions import SessionMiddleware

from api_services.app.routers.auth import router as auth_router
from api_services.app.routers.query import router as query_router
from api_services.app.routers.health import router as health_router

from api_services.app.config import FRONTEND_URL
from ai_services.agents.rag_agent.agent import RAGQueryAgent
from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from api_services.app.models.app_dependencies import AppDependencies
from ai_services.retrievers.retriever_factory import create_retrievers
from shared.utils.env_helper import require_env
from shared.utils.llm_utils import AzureOpenAILLM


@asynccontextmanager
async def lifespan(app: FastAPI):
    database = require_env("NEO4J_DATABASE") or None

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

    app.state.deps = AppDependencies(
        driver=driver,
        client=client,
        embedder=embedder,
        llm=llm,
        chat_llm=chat_llm,
        entity_retriever=entity_retriever,
        chunk_retriever=chunk_retriever,
        community_retriever=community_retriever,
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
