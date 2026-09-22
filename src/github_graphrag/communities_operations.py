import asyncio

from neo4j import Driver

from github_graphrag.summarize_communities import generate_community_summary


GRAPH_NAME = "entityGraph"

def get_relationship_types(driver, database: str) -> list[str]:
    result = driver.execute_query(
        """
        MATCH (:__Entity__)-[r]-(:__Entity__)
        RETURN DISTINCT type(r) AS relationship_type
        ORDER BY relationship_type
        """,
        database_=database,
    )

    return [
        record["relationship_type"]
        for record in result.records
    ]

def create_projection(driver: Driver, database: str):
    relationship_types = get_relationship_types(driver, database)
    
    if not relationship_types:
        raise RuntimeError(
            "No relationships found between __Entity__ nodes."
        )

    result = driver.execute_query(
        """
        CALL gds.graph.project(
            $graph_name,
            '__Entity__',
            $relationship_types,
            {
                undirectedRelationshipTypes: $relationship_types,
                memory: $memory,
                ttl: $ttl
            }
        )
        YIELD graphName, nodeCount, relationshipCount
        RETURN graphName, nodeCount, relationshipCount
        """,
        graph_name=GRAPH_NAME,
        relationship_types=relationship_types,
        memory="2GB",
        ttl="PT30M",
        database_=database,
    )

    return result.records[0]
    
def ensure_projection(driver: Driver, database: str):
    result = driver.execute_query(
        """
        CALL gds.graph.exists($graph_name)
        YIELD exists
        RETURN exists
        """,
        graph_name=GRAPH_NAME,
        database_=database,
    )

    exists = result.records[0]["exists"]

    if exists:
        driver.execute_query(
            """
            CALL gds.graph.drop($graph_name)
            YIELD graphName
            """,
            graph_name=GRAPH_NAME,
            database_=database,
        )

    return create_projection(driver, database)

def detect_communities(driver: Driver, database: str):
    result = driver.execute_query(
        """
        CALL gds.leiden.write(
            $graph_name,
            {
                writeProperty: 'communityId',
                randomSeed: 42
            }
        )
        YIELD communityCount, modularity, nodeCount
        RETURN communityCount, modularity, nodeCount
        """,
        graph_name=GRAPH_NAME,
        database_=database,
    )

    return result.records[0]

def get_communities(driver: Driver, database: str):
    result = driver.execute_query(
        """
        MATCH (e:__Entity__)
        WHERE e.communityId IS NOT NULL
        RETURN
            e.communityId AS community_id,
            collect(e.name) AS entities
        ORDER BY community_id
        """,
        database_=database,
    )

    return result.records

def load_communities(driver, database: str):
    result = driver.execute_query(
        """
        MATCH (e:__Entity__)
        WHERE e.communityId IS NOT NULL

        OPTIONAL MATCH (e)-[r]->(neighbor:__Entity__)
        WHERE neighbor.communityId = e.communityId

        WITH
            e.communityId AS community_id,
            collect(DISTINCT e.name) AS entities,
            collect(
                DISTINCT CASE
                    WHEN r IS NOT NULL
                    THEN e.name + " -[" + type(r) + "]-> " + neighbor.name
                END
            ) AS relationships

        RETURN
            community_id,
            entities,
            [x IN relationships WHERE x IS NOT NULL] AS relationships

        ORDER BY community_id
        """,
        database_=database,
    )

    return result.records

def save_community(
    driver,
    database: str,
    community_id: int,
    entities: list[str],
    summary: str,
):
    driver.execute_query(
        """
        MERGE (c:Community {communityId: $community_id})
        SET
            c.summary = $summary,
            c.entityCount = $entity_count

        WITH c

        MATCH (e:__Entity__)
        WHERE e.communityId = $community_id

        MERGE (e)-[:MEMBER_OF]->(c)
        """,
        community_id=community_id,
        entity_count=len(entities),
        summary=summary,
        database_=database,
    )

async def main():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    from github_graphrag.graphdb_driver import CreateDriver
    from neo4j_graphrag.llm import GeminiLLM

    uri = os.getenv("NEO4J_URI")
    username = os.getenv("NEO4J_USERNAME")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE") or None
    api_key = os.getenv("GEMINI_API_KEY")

    driver = CreateDriver(uri, username, password).get_driver()
    
    llm = GeminiLLM(
        model_name=os.getenv("GEMINI_LLM_MODEL", "gemini-3.8-flash"),
        api_key=api_key,
    )

    try:
        driver.verify_connectivity()
        
        communities = load_communities(driver, database)
        for community in communities:
            community_id = community["community_id"]
            entities = community["entities"]
            relationships = community["relationships"]

            summary = await generate_community_summary(
                llm,
                community_id,
                entities,
                relationships,
            )

            save_community(
                driver,
                database,
                community_id,
                entities,
                summary,
            )

            print(f"Saved community {community_id}")

        # ensure_projection(driver, database)
        
        # stats = detect_communities(driver, database)
        # print(
        #     f"Detected {stats['communityCount']} communities "
        #     f"across {stats['nodeCount']} entities"
        # )

        # communities = get_communities(driver, database)

        # for community in communities:
        #     print(
        #         community["community_id"],
        #         community["entities"]
        #     )
    finally:
        driver.close()

if __name__ == "__main__":
    asyncio.run(main())