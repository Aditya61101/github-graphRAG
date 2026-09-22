import os
import asyncio
from dotenv import load_dotenv
load_dotenv()

from neo4j_graphrag.embeddings import GeminiEmbedder
from neo4j_graphrag.llm import GeminiLLM
from neo4j_graphrag.retrievers import VectorRetriever

from github_graphrag.graph_expansion import expand_entities
from github_graphrag.graphdb_driver import CreateDriver

def format_graph_context(graph_records: list[dict]) -> str:
    lines = []

    for record in graph_records:
        source = record["source"]
        relationship = record["relationship"]
        neighbor = record["neighbor"]

        if relationship is None:
            lines.append(
                f"- Entity: {source}"
            )
        else:
            lines.append(
                f"- {source} -[{relationship}]-> {neighbor}"
            )

    return "\n".join(lines)

async def generate_answer(
    llm: GeminiLLM,
    question: str,
    graph_context: str,
) -> str:

    prompt = f"""
You are an assistant answering questions about a software repository.

Answer the user's question using ONLY the supplied graph context.

If the context does not contain enough information, explicitly say
that the repository graph does not provide enough information.

Do not invent classes, services, relationships, or implementation details.

USER QUESTION:
{question}

GRAPH CONTEXT:
{graph_context}

Provide a concise but useful technical answer.
"""

    response = await llm.ainvoke(prompt)
    return response.content


def required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"{name} is required.")

    return value


from neo4j_graphrag.types import RetrieverResultItem
import neo4j

def entity_result_formatter(record: neo4j.Record) -> RetrieverResultItem:
    node = record["node"]
    score = record["score"]

    return RetrieverResultItem(
        content=node["name"],
        metadata={
            "score": score,
        },
    )

async def main() -> None:
    api_key = required_env("GEMINI_API_KEY")

    uri = required_env("NEO4J_URI")
    username = required_env("NEO4J_USERNAME")
    password = required_env("NEO4J_PASSWORD")

    database = os.getenv("NEO4J_DATABASE") or None

    driver = CreateDriver(uri, username, password).get_driver()

    embedder = GeminiEmbedder(
        model="gemini-embedding-001",
        embedding_dim=768,
        api_key=api_key,
    )
    
    llm = GeminiLLM(
        model_name=os.getenv("GEMINI_LLM_MODEL", "gemini-3.8-flash"),
        api_key=api_key,
    )

    try:
        driver.verify_connectivity()

        retriever = VectorRetriever(
            driver=driver,
            index_name="entity_vector_index",
            embedder=embedder,
            return_properties=["name"],
            result_formatter=entity_result_formatter
        )

        query = "Which components are responsible for user authentication?"

        results = retriever.search(
            query_text=query,
            top_k=5,
        )
        
        entity_names = [
            item.content
            for item in results.items
        ]
        print("entity names: ", entity_names)

        graph_records = expand_entities(
            driver,
            entity_names,
            database,
        )
        
        context = format_graph_context(graph_records)

        answer = await generate_answer(
            llm,
            question=query,
            graph_context=context
        )

        print("\nAnswer:", answer)

    finally:
        driver.close()

if __name__ == "__main__":
    asyncio.run(main())