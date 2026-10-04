from dataclasses import dataclass

from github_graphrag.retrievers.graph_expansion import (
    expand_entities,
    load_entity_evidence_ids,
    load_evidence_chunks,
)
from github_graphrag.retrievers.context_formatter import format_retrieval_context


@dataclass(frozen=True)
class HybridRetrievalResult:
    context: str
    sources: dict[str, list[dict]]


def load_communities(driver, database: str, community_ids: list[str | int]):
    if not community_ids:
        return []

    result = driver.execute_query(
        """
        MATCH (c:Community)
        WHERE c.communityId IN $community_ids
        CALL {
            WITH c
            OPTIONAL MATCH (e:Entity)-[:MEMBER_OF]->(c)
            RETURN collect(DISTINCT CASE WHEN e IS NULL THEN NULL ELSE {
                canonical_id: e.id,
                label: e.label,
                name: e.name
            } END) AS raw_members
        }
        CALL {
            WITH c
            OPTIONAL MATCH (source:Entity)-[:MEMBER_OF]->(c)
            OPTIONAL MATCH (source)-[r]->(target:Entity)-[:MEMBER_OF]->(c)
            WHERE r IS NULL OR type(r) <> "MEMBER_OF"
            RETURN collect(DISTINCT CASE WHEN r IS NULL THEN NULL ELSE {
                source_id: source.id,
                source_label: source.label,
                source_name: source.name,
                relationship: type(r),
                target_id: target.id,
                target_label: target.label,
                target_name: target.name
            } END) AS raw_relationships
        }
        RETURN
            c.communityId AS community_id,
            c.summary AS summary,
            [member IN raw_members WHERE member IS NOT NULL] AS members,
            [relationship IN raw_relationships WHERE relationship IS NOT NULL] AS relationships
        ORDER BY community_id
        """,
        community_ids=community_ids,
        database_=database,
    )
    return result.records


def _evidence_records(chunks: list[dict], ids_by_key: dict) -> dict:
    by_id = {chunk["chunk_id"]: chunk for chunk in chunks if chunk.get("chunk_id")}
    return {
        key: [by_id[cid] for cid in chunk_ids if cid in by_id]
        for key, chunk_ids in ids_by_key.items()
    }


async def hybrid_retrieve(
    query: str,
    driver,
    database: str,
    embedder,
    entity_retriever,
    community_retriever,
    chunk_retriever,
    entity_top_k: int = 5,
    community_top_k: int = 3,
    chunk_top_k: int = 5,
):
    vectors = await embedder.embed([query])
    if len(vectors) != 1:
        raise RuntimeError("Embedder must return exactly one vector for a query.")
    query_vector = vectors[0]

    entity_results = entity_retriever.search(query_vector=query_vector, top_k=entity_top_k)
    entity_ids = [
        item.metadata["canonical_id"]
        for item in entity_results.items
        if item.metadata.get("canonical_id")
    ]

    graph_records = expand_entities(driver, entity_ids, database)
    print("GRAPH RECORDS:")
    for record in graph_records:
        if (
            record["source"] == "POST /login"
            and record["target"] == "User"
        ):
            print(record)
    community_results = community_retriever.search(
        query_vector=query_vector,
        top_k=community_top_k,
    )
    community_ids = [item.metadata["community_id"] for item in community_results.items]
    communities = load_communities(driver, database, community_ids)

    # Direct semantic retrieval over raw source-code chunks. This is the
    # evidence path: it gives the LLM actual code/text, not only graph nodes.
    chunk_results = chunk_retriever.search(query_vector=query_vector, top_k=chunk_top_k)
    print("chunk results: ", chunk_results.items[0].metadata)
    
    # Resolve provenance for graph facts. Entity and relationship evidence are
    # represented by stable chunk IDs, which are then expanded to file/excerpt.
    entity_chunk_ids = load_entity_evidence_ids(driver, entity_ids, database)
    # print("ENTITY EVIDENCE IDS:",entity_chunk_ids)
    
    relationship_chunk_ids = {}
    for record in graph_records:
        key = (
            f"{record['source']} -[{record['relationship']}]-> "
            f"{record['target']}"
        )
        relationship_chunk_ids[key] = list(
            dict.fromkeys(record.get("relationship_evidence_chunk_ids") or [])
        )

    provenance_chunk_ids = list(dict.fromkeys(
        [cid for ids in entity_chunk_ids.values() for cid in ids]
        + [cid for ids in relationship_chunk_ids.values() for cid in ids]
    ))
    provenance_chunks = load_evidence_chunks(driver, provenance_chunk_ids, database)

    entity_evidence = _evidence_records(provenance_chunks, entity_chunk_ids)
    relationship_evidence = _evidence_records(provenance_chunks, relationship_chunk_ids)

    context = format_retrieval_context(
        entity_results=entity_results,
        community_results=community_results,
        communities=communities,
        graph_records=graph_records,
        chunk_results=chunk_results,
        entity_evidence=entity_evidence,
        relationship_evidence=relationship_evidence,
    )

    return HybridRetrievalResult(
        context=context,
        sources={
            "entities": [
                {
                    "canonical_id": item.metadata.get("canonical_id"),
                    "label": item.metadata.get("label"),
                    "name": item.metadata.get("name", item.content),
                    "score": item.metadata.get("score"),
                    "evidence": entity_evidence.get(
                        item.metadata.get("canonical_id"), []
                    ),
                }
                for item in entity_results.items
            ],
            "communities": [
                {
                    "community_id": item.metadata.get("community_id"),
                    "score": item.metadata.get("score"),
                }
                for item in community_results.items
            ],
            "relationships": [
                {
                    "source_id": record["source_id"],
                    "source": record["source"],
                    "relationship": record["relationship"],
                    "target_id": record["target_id"],
                    "target": record["target"],
                    "evidence": relationship_evidence.get(
                        f"{record['source']} -[{record['relationship']}]-> {record['target']}",
                        [],
                    ),
                }
                for record in graph_records
            ],
            "chunks": [
                {
                    "chunk_id": item.metadata.get("chunk_id"),
                    "file_path": item.metadata.get("file_path"),
                    "repository": item.metadata.get("repository"),
                    "commit": item.metadata.get("commit"),
                    "chunk_index": item.metadata.get("chunk_index"),
                    "score": item.metadata.get("score"),
                    "excerpt": item.content,
                }
                for item in chunk_results.items
            ],
        },
    )
