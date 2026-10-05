from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from api_services.app.routers.auth import router as auth_router
from api_services.app.routers.query import router as query_router

from api_services.app.config import FRONTEND_URL

# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     database = require_env("NEO4J_DATABASE") or None

#     driver = GraphDatabase.driver(
#         require_env("NEO4J_URI"),
#         auth=(
#             require_env("NEO4J_USERNAME"),
#             require_env("NEO4J_PASSWORD"),
#         ),
#     )

#     client = AsyncAzureOpenAI(
#         azure_endpoint=require_env("AZURE_OPENAI_ENDPOINT"),
#         api_key=require_env("AZURE_OPENAI_API_KEY"),
#         api_version=require_env("AZURE_OPENAI_API_VERSION"),
#     )

#     embedder = AzureOpenAIEmbedder(...)
#     llm = AzureOpenAILLM(...)

#     entity_retriever, community_retriever, chunk_retriever = create_retrievers(
#         driver=driver,
#         database=database,
#     )

#     app.state.rag_dependencies = RAGDependencies(
#         driver=driver,
#         database=database,
#         embedder=embedder,
#         llm=llm,
#         entity_retriever=entity_retriever,
#         community_retriever=community_retriever,
#         chunk_retriever=chunk_retriever,
#     )

#     app.state.rag_agent = RAGQueryAgent(
#         dependencies=app.state.rag_dependencies,
#     )

#     yield

#     driver.close()


app = FastAPI()

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