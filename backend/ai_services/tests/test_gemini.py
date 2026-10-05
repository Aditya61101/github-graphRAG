import os
from neo4j import GraphDatabase
from dotenv import load_dotenv
load_dotenv()

gemini_api_key = os.environ['GEMINI_API_KEY']

# llm test
from neo4j_graphrag.llm import GeminiLLM

llm = GeminiLLM(
    model_name="gemini-3.8-flash",
)
response = llm.invoke("Say hello")
print(response)

# embedder test
from neo4j_graphrag.embeddings import GeminiEmbedder

embedder = GeminiEmbedder(
    model="gemini-embedding-001"
)

vector = embedder.embed_query("Hello world")
print(len(vector))