from dotenv import load_dotenv
load_dotenv()

def expand_entities(
    driver,
    entity_ids: list[str],
    database: str | None,
) -> list[dict]:
    """Return directed one-hop architectural edges adjacent to canonical IDs."""
    if not entity_ids:
        return []

    records, _, _ = driver.execute_query(
        """
        MATCH (source:Entity)-[r]->(target:Entity)
        WHERE source.id IN $entity_ids OR target.id IN $entity_ids

        RETURN
            source.id AS source_id,
            source.label AS source_label,
            source.name AS source,
            type(r) AS relationship,
            target.id AS target_id,
            target.label AS target_label,
            target.name AS target
        ORDER BY source, relationship, target
        """,
        entity_ids=list(dict.fromkeys(entity_ids)),
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
