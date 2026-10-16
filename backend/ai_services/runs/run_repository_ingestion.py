"""Existing container command: python -m ai_services.runs.run_repository_ingestion."""
import asyncio
import os
import httpx
from neo4j import GraphDatabase
from openai import AsyncAzureOpenAI
from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from ai_services.github_app.service import GitHubAppService
from ai_services.github_app.settings import GitHubAppSettings
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs
from ai_services.ingestion.sources.github import GitHubRepositorySource
from ai_services.ingestion.graph_control import GraphControl
from ai_services.ingestion.worker import IngestionWorker
from shared.utils.llm_utils import AzureOpenAILLM
from shared.utils.processing import require_background_processing


async def run():
    require_background_processing()
    store = SqliteApplicationStore()
    if store.session_factory.kw["bind"].dialect.name != "postgresql":
        raise RuntimeError("Remote ingestion requires shared PostgreSQL")
    driver = GraphDatabase.driver(os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]))
    client = AsyncAzureOpenAI(azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"])
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            control = GraphControl(driver, os.environ.get("NEO4J_DATABASE", "neo4j"))
            control.initialize()
            github = GitHubAppService(store, GitHubAppSettings.from_env(), http)
            worker = IngestionWorker(IngestionJobs(store.session_factory), store,
                GitHubRepositorySource(os.environ.get("TMPDIR", "/tmp")), github, control,
                llm=AzureOpenAILLM(client, os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]),
                embedder=AzureOpenAIEmbedder(client,
                    deployment=os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"],
                    dimensions=int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072"))))
            await worker.run(os.environ["INGESTION_RUN_ID"])
    finally:
        await client.close()
        driver.close()


if __name__ == "__main__":
    asyncio.run(run())
