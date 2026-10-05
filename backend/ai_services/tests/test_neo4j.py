import os
from neo4j import GraphDatabase
from dotenv import load_dotenv
load_dotenv()

username = os.getenv("NEO4J_USERNAME")
password = os.getenv("NEO4J_PASSWORD")

driver = GraphDatabase.driver(
    uri=os.getenv("NEO4J_URI"),
    auth=(username,password)
)
driver.verify_connectivity()
print("neo4j connected")