from dotenv import load_dotenv
load_dotenv()

def expand_entities(
    driver,
    entity_names: list[str],
    database: str | None,
) -> list[dict]:

    records, _, _ = driver.execute_query(
        """
        MATCH (source:__Entity__)
        WHERE source.name IN $entity_names

        OPTIONAL MATCH (source)-[r]-(target:__Entity__)

        RETURN
            source.name AS source,
            type(r) AS relationship,
            neighbor.name AS neighbor
        ORDER BY source, relationship, neighbor
        """,
        entity_names=entity_names,
        database_=database,
    )

    return [dict(record) for record in records]

# if __name__ == "__main__":
#     entity_names = [
#         "AuthService",
#         "User",
#         "UserRepository",
#         "Password",
#         "JWT",
#     ]
#     driver = CreateDriver(
#         uri=os.getenv("NEO4J_URI"),
#         username=os.getenv("NEO4J_USERNAME"),
#         password=os.getenv("NEO4J_PASSWORD"),
#     ).get_driver()

#     graph_context = expand_entities(
#         driver,
#         entity_names,
#         os.getenv("NEO4J_DATABASE") or None,
#     )

#     for item in graph_context:
#         print(item)