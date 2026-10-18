"""Dedicated Job: python -m ai_services.runs.run_adr_ingestion."""
import asyncio
import os
import tempfile
import httpx
from neo4j import GraphDatabase
from openai import AsyncAzureOpenAI
from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from ai_services.github_app.service import GitHubAppService
from ai_services.github_app.settings import GitHubAppSettings
from ai_services.ingestion.adr.chunker import ADRChunker
from ai_services.ingestion.adr.extractor import ADRArchitecturalExtractor
from ai_services.ingestion.adr.jobs import ADRJobs
from ai_services.ingestion.adr.neo4j_writer import ADRNeo4jWriter
from ai_services.ingestion.adr.originals import original_storage
from ai_services.ingestion.adr.processor import ADRProcessingService
from ai_services.ingestion.adr.resolver import ADREntityResolver
from ai_services.ingestion.adr.worker import ADRWorker
from ai_services.ingestion.graph_control import GraphControl
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from shared.utils.llm_utils import AzureOpenAILLM
from shared.utils.processing import require_background_processing


async def run():
    require_background_processing()
    if not os.getenv('ADR_GCS_BUCKET'):
        raise RuntimeError('ADR ingestion requires ADR_GCS_BUCKET')
    store = SqliteApplicationStore()
    if store.session_factory.kw['bind'].dialect.name != 'postgresql':
        raise RuntimeError('Remote ADR ingestion requires shared PostgreSQL')
    database = os.environ['NEO4J_DATABASE']
    dimensions = int(os.getenv('AZURE_OPENAI_EMBEDDING_DIMENSIONS', '3072'))
    driver = GraphDatabase.driver(os.environ['NEO4J_URI'],
        auth=(os.environ['NEO4J_USERNAME'], os.environ['NEO4J_PASSWORD']))
    try:
        async with httpx.AsyncClient(timeout=30) as http, AsyncAzureOpenAI(
            azure_endpoint=os.environ['AZURE_OPENAI_ENDPOINT'], api_key=os.environ['AZURE_OPENAI_API_KEY'],
            api_version=os.environ['AZURE_OPENAI_API_VERSION']) as client:
            embedder = AzureOpenAIEmbedder(client,
                deployment=os.environ['AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME'], dimensions=dimensions)
            llm = AzureOpenAILLM(client, os.environ['AZURE_OPENAI_DEPLOYMENT_NAME'])
            control = GraphControl(driver, database)
            control.initialize()
            writer = ADRNeo4jWriter(driver, database, dimensions)
            writer.initialize_constraints()
            processor = ADRProcessingService(ADRChunker(), ADRArchitecturalExtractor(llm),
                ADREntityResolver(driver, database, embedder, dimensions), writer, embedder, dimensions)
            with tempfile.TemporaryDirectory(prefix='decisionguard-adr-original-') as scratch:
                worker = ADRWorker(ADRJobs(store.session_factory), store, original_storage(scratch), processor,
                    control, llm=llm, embedder=embedder,
                    github=GitHubAppService(store, GitHubAppSettings.from_env(), http),
                    max_file_size=int(os.getenv('MAX_ADR_FILE_SIZE_BYTES', str(10 * 1024 * 1024))))
                await worker.run(os.environ['INGESTION_RUN_ID'])
    finally:
        driver.close()


if __name__ == '__main__':
    asyncio.run(run())
