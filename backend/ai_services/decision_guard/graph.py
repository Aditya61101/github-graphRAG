from contextlib import contextmanager

from neo4j import READ_ACCESS

from .ports import Page


class GraphReadError(RuntimeError):
    pass


# Each query returns explicit repository identities for defensive adapter validation.
ENTITY = "{ .id, .name, .label, .repository, .source, .aliases }"
CHUNK = (
    "{ .id, .repository, .commit, .filePath, .text, .contentHash, .adr_id, .section }"
)
ADR = "{ .id, .repository, .content_hash, .source_name, .source_type, .source_url, .source_version }"

FILE_CHUNKS = f"""
UNWIND $paths AS path
MATCH (f:File {{key: $repo + ':' + path, repository: $repo}})
MATCH (f)-[:HAS_CHUNK]->(c:Chunk {{repository: $repo}})
WHERE NOT c:ADRChunk
WITH DISTINCT f, c ORDER BY f.path, c.id LIMIT $limit
OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity {{repository: $repo}})
RETURN f.path AS path, c {CHUNK} AS chunk, collect(DISTINCT e {ENTITY}) AS entities
ORDER BY path, chunk.id
"""

RELATIONSHIPS = f"""
MATCH (s:Entity {{repository: $repo}})-[:ASSERTS]->(a:GraphAssertion {{repository: $repo}})
      -[:TARGETS]->(t:Entity {{repository: $repo}})
WHERE s.id IN $entity_ids OR t.id IN $entity_ids
WITH DISTINCT s, a, t ORDER BY a.id LIMIT $limit
OPTIONAL MATCH (c:Chunk {{repository: $repo}})-[:SUPPORTS]->(a)
OPTIONAL MATCH (adr:ADR {{repository: $repo}})-[:HAS_CHUNK]->(c)
WHERE c:ADRChunk AND c.adr_id = adr.id AND a.source_id = adr.id
RETURN s {ENTITY} AS source, t {ENTITY} AS target,
       a {{ .id, .repository, .relationshipType, .confidence, .rationale, .source_type, .source_id }} AS assertion,
       collect(DISTINCT {{chunk: c {CHUNK}, adr: adr {ADR}, is_adr: c:ADRChunk}}) AS support
UNION ALL
MATCH (s:Entity {{repository: $repo}})-[r]->(t:Entity {{repository: $repo}})
WHERE (s.id IN $entity_ids OR t.id IN $entity_ids)
  AND NOT EXISTS {{ MATCH (s)-[:ASSERTS]->(a:GraphAssertion {{repository: $repo}})-[:TARGETS]->(t)
                   WHERE a.relationshipType = type(r) }}
WITH s, r, t ORDER BY s.id, type(r), t.id LIMIT $limit
RETURN s {ENTITY} AS source, t {ENTITY} AS target,
       {{id: s.id + '|' + type(r) + '|' + t.id, repository: $repo,
         relationshipType: type(r), confidence: r.confidence, rationale: r.rationale}} AS assertion,
       [] AS support
"""

CATALOG = f"""
MATCH (adr:ADR {{repository: $repo}})
RETURN adr {ADR} AS adr ORDER BY adr.id LIMIT $limit
"""

COMMUNITIES = f"""
UNWIND $entity_ids AS entity_id
MATCH (e:Entity {{id: entity_id, repository: $repo}})-[:MEMBER_OF]->(c:Community {{repository: $repo}})
RETURN DISTINCT e {ENTITY} AS entity,
       c {{ .communityId, .repository, .name, .title, .architecturalRole, .aliases }} AS community
ORDER BY entity.id, community.communityId LIMIT $limit
"""

SCOPE_EVIDENCE = f"""
UNWIND $chunk_ids AS chunk_id
MATCH (adr:ADR {{repository: $repo}})-[:HAS_CHUNK]->(ch:Chunk:ADRChunk {{id: chunk_id, repository: $repo}})
WHERE adr.id IN $adr_ids AND ch.adr_id = adr.id
RETURN DISTINCT adr {ADR} AS adr, ch {CHUNK} AS chunk
ORDER BY adr.id, chunk.id LIMIT $limit
"""

# Bound candidate nodes before evidence expansion; full original supporting text is
# loaded for those nodes and subsequently capped with explicit evidence limitations.
REQUIREMENTS = f"""
MATCH (adr:ADR {{repository: $repo}})-[:DEFINES]->(k)
WHERE k.repository = $repo AND (k:ArchitecturalDecision OR k:ArchitecturalConstraint)
  AND ($fallback OR EXISTS {{ MATCH (k)-[:AFFECTS|CONSTRAINS]->(e:Entity {{repository: $repo}})
                            WHERE e.id IN $entity_ids }}
       OR any(x IN coalesce(k.applicability_entity_ids, []) WHERE x IN $entity_ids)
       OR any(x IN coalesce(k.applicability_community_ids, []) WHERE x IN $community_ids)
       OR coalesce(k.applicability_repository_wide, false)
       OR 'repository' IN coalesce(k.applicability_scopes, [])
       OR any(x IN coalesce(k.applicability_languages, []) WHERE toLower(x) IN $languages)
       OR EXISTS {{ MATCH (adr)-[:HAS_CHUNK]->(ch:ADRChunk {{repository: $repo}})-[:MENTIONS]->(e:Entity {{repository: $repo}})
                   WHERE ch.adr_id = adr.id AND e.id IN $entity_ids }})
WITH DISTINCT k,
     EXISTS {{ MATCH (adr)-[:HAS_CHUNK]->(ch:ADRChunk {{repository: $repo}})-[:MENTIONS]->(e:Entity {{repository: $repo}})
               WHERE ch.adr_id = adr.id AND e.id IN $entity_ids }} AS adr_mention,
     CASE WHEN EXISTS {{ MATCH (k)-[:AFFECTS|CONSTRAINS]->(e:Entity {{repository: $repo}})
                         WHERE e.id IN $entity_ids }} THEN 8 ELSE 0 END +
     CASE WHEN any(x IN coalesce(k.applicability_entity_ids, []) WHERE x IN $entity_ids) THEN 4 ELSE 0 END +
     CASE WHEN any(x IN coalesce(k.applicability_community_ids, []) WHERE x IN $community_ids) THEN 3 ELSE 0 END +
     CASE WHEN coalesce(k.applicability_repository_wide, false) THEN 2 ELSE 0 END +
     CASE WHEN any(x IN coalesce(k.applicability_languages, []) WHERE toLower(x) IN $languages) THEN 1 ELSE 0 END +
     CASE WHEN k.applicability_json IS NULL OR coalesce(k.applicability_unresolved, true) THEN 1 ELSE 0 END AS hint_score
ORDER BY hint_score DESC, k.id LIMIT $limit
OPTIONAL MATCH (k)-[link:AFFECTS|CONSTRAINS]->(e:Entity {{repository: $repo}})
WITH k, hint_score, adr_mention, collect(DISTINCT e {{ .id, .name, .label, .repository, .source, .aliases, relationship: type(link) }}) AS targets
MATCH (adr:ADR {{repository: $repo}})-[:DEFINES]->(k)
OPTIONAL MATCH (adr)-[:HAS_CHUNK]->(ch:Chunk:ADRChunk {{repository: $repo}})-[:SUPPORTS]->(k)
WHERE ch.adr_id = adr.id
RETURN properties(k) AS knowledge,
       CASE WHEN k:ArchitecturalDecision THEN 'DECISION' ELSE 'CONSTRAINT' END AS kind,
       targets, hint_score, adr_mention,
       collect(DISTINCT {{adr: adr {ADR}, chunk: ch {CHUNK}}}) AS support
ORDER BY hint_score DESC, knowledge.id
"""


class Neo4jArchitectureGraphReader:
    """Read transaction only: no schema creation, ingestion or application models."""

    def __init__(self, driver, database: str):
        self.driver, self.database = driver, database

    @contextmanager
    def read_snapshot(self, repository_id: str):
        if not repository_id:
            raise ValueError("Repository scope is required")
        with (
            self.driver.session(
                database=self.database, default_access_mode=READ_ACCESS
            ) as session,
            session.begin_transaction() as tx,
        ):
            yield _ScopedGraph(tx, repository_id)


class _ScopedGraph:
    def __init__(self, tx, repository_id):
        self.tx, self.repo = tx, repository_id

    def _read(self, query, limit, **params):
        try:
            rows = tuple(
                record.data()
                for record in self.tx.run(
                    query, repo=self.repo, limit=limit + 1, **params
                )
            )
        except Exception as exc:  # noqa: BLE001 - normalize driver/backend failures without leaking source or credentials
            # Sanitized errors retain type without leaking query/source/credentials.
            raise GraphReadError(type(exc).__name__) from None
        return Page(rows[:limit], len(rows) > limit)

    def file_chunks(self, paths, limit):
        return self._read(FILE_CHUNKS, limit, paths=list(paths))

    def relationships(self, entity_ids, limit):
        return self._read(
            RELATIONSHIPS + "\nORDER BY assertion.id LIMIT $limit",
            limit,
            entity_ids=list(entity_ids),
        )

    def adr_catalog(self, limit):
        return self._read(CATALOG, limit)

    def communities(self, entity_ids, limit):
        return self._read(COMMUNITIES, limit, entity_ids=list(entity_ids))

    def applicability_evidence(self, adr_ids, chunk_ids, limit):
        return self._read(
            SCOPE_EVIDENCE, limit, adr_ids=list(adr_ids), chunk_ids=list(chunk_ids)
        )

    def requirements(
        self, entity_ids, limit, *, fallback=False, community_ids=(), languages=()
    ):
        return self._read(
            REQUIREMENTS,
            limit,
            entity_ids=list(entity_ids),
            fallback=fallback,
            community_ids=list(community_ids),
            languages=list(languages),
        )
