

import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from github_graphrag.graphdb_driver import CreateDriver

# from neo4j_graphrag.llm import GeminiLLM
# from neo4j_graphrag.embeddings import GeminiEmbedder
# from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from neo4j_graphrag.embeddings import SentenceTransformerEmbeddings
from neo4j_graphrag.llm import AzureOpenAILLM
from neo4j_graphrag.embeddings import AzureOpenAIEmbeddings

from github_graphrag.ingestion.ingest import IngestionRunner, ingest_plan
from github_graphrag.ingestion.read_write_plan import load_plan
from github_graphrag.tests.test_ingestion import repo

ingestion_plan = load_plan()

def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required. Copy .env.example and set its value.")
    return value

async def main():
    # api_key = required_env("GEMINI_API_KEY")
    uri = required_env("NEO4J_URI")
    username = required_env("NEO4J_USERNAME")
    password = required_env("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE") or None
    driver = CreateDriver(uri, username, password).get_driver()
    
    llm = AzureOpenAILLM(
        model_name=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ["OPENAI_API_VERSION"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
    )
    embedder = SentenceTransformerEmbeddings(
        model="all-MiniLM-L6-v2"
    )
    # embedder = AzureOpenAIEmbeddings(
    #     model=os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"],
    #     azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    #     api_version=os.environ["OPENAI_API_VERSION"],
    #     api_key=os.environ["AZURE_OPENAI_API_KEY"],
    # )
    try:
        driver.verify_connectivity()
        runner = IngestionRunner(
            llm=llm,
            driver=driver,
            embedder=embedder,
            database=database,
        )
        await ingest_plan(
            repo_root=repo.root,
            ingestion_plan=ingestion_plan,
            runner=runner,
        )
    finally:
        driver.close()
        await llm.aclose()

if __name__ == "__main__":
    asyncio.run(main())
# chunks = build_chunks(
#     repo_root=repo.root,
#     plan=plan,
# )
# save_chunk_report(chunks)
# print(
#     f"Built {len(chunks)} chunks. "
#     f"Report written to {CHUNK_REPORT_FILE}"    
# )