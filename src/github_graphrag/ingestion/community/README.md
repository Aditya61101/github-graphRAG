# Community Layer for Repository Knowledge Graph (RKG)

The **Community Layer** processes an existing architectural knowledge graph in Neo4j into hierarchically coherent clusters (communities), synthesizes structured architectural summaries using an LLM, generates vector embeddings for each community, and indexes them in a dedicated Neo4j vector index.

This layer bridges fine-grained entity-level code knowledge with macro-level software architecture comprehension for **Hybrid GraphRAG**.

---

## 1. Architecture & Data Flow

```text
Existing Neo4j Architectural Graph (:__Entity__ nodes & directed edges)
            |
            v
    Graph Projection (GDS in-memory projection with undirected edges, excluding MEMBER_OF)
            |
            v
    Community Detection (Leiden or Louvain via Neo4j GDS writing temporary _gdsCommunityId)
            |
            v
    Identity Resolution (Derive final community IDs; detect collisions)
            |
            v
    Membership Synchronization (:__Entity__ -[:MEMBER_OF]-> :Community; remove _gdsCommunityId)
            |
            v
    Context Loading (Internal directed relationships: e1 -[r]-> e2; external edges excluded)
            |
            v
    Independent State Evaluation (Context Hash, Summary Hash, Embedded Version Hash)
            |
            v
    Community Summarization (Structured architectural LLM summaries; skipped if context unchanged)
            |
            v
    Community Embeddings (Deterministic summary embeddings via Embedder; skipped if current)
            |
            v
    Neo4j Vector Index (Validation & readiness polling: :Community.embedding vector index)
```

---

## 2. Package Structure

```text
src/github_graphrag/community/
├── __init__.py          # Public package API
├── models.py            # Pydantic domain models (Context, Summary, Member, Embedding, ProcessingState)
├── interfaces.py        # Abstract protocols (Detector, Store, Summarizer, Embedder, IndexManager)
├── config.py            # Configuration with Cypher safety validation and timeouts
├── projection.py        # Neo4j GDS in-memory projection manager (excludes non-architectural edges)
├── detector.py          # Leiden & Louvain GDS detectors + safe identity strategies
├── membership.py        # Idempotent Neo4j community store, membership sync, and context loader
├── summarizer.py        # LLM architectural summarizer with key entity validation
├── embedder.py          # Batch embedding generator via project Embedder protocol + version hashing
├── vector_index.py      # Vector index manager with full metadata validation and await_online
├── pipeline.py          # High-level orchestrator with decoupled summary/embedding evaluation
├── exceptions.py        # Context-aware custom error hierarchy
└── README.md            # Comprehensive architecture, diagnostics, and usage guide
```

---

## 3. How Community Detection Works

1. **Relationship Discovery**: Discovers distinct relationship types connecting `__Entity__` nodes in the database, strictly excluding `MEMBER_OF` and any other non-architectural relationships.
2. **GDS Projection**: Temporary in-memory projection of the entity graph created via `gds.graph.project`.
3. **Algorithm Execution**: Executes Leiden (`gds.leiden.write`) or Louvain with a deterministic `randomSeed: 42`, writing raw partition IDs to a temporary node property `_gdsCommunityId`.
4. **Identity Strategy**: Transforms raw GDS partition IDs into final application community IDs via pluggable `CommunityIdentityStrategy` implementations (`SnapshotCommunityIdStrategy` or `DeterministicCommunityIdStrategy`).
5. **Collision Protection**: Strictly validates that all raw partitions produce unique final community IDs.
6. **Membership Synchronization**: Sets `e.communityId = final_id`, deletes stale `MEMBER_OF` relationships, removes `_gdsCommunityId`, merges `(:Community)`, and links `(e)-[:MEMBER_OF]->(c)`.
7. **Integrity Validation**: Verifies that every projected entity belongs to exactly one community and cleans up empty communities during full synchronization.

---

## 4. Why GDS Projection is Undirected While Relationships Remain Directed

In software architecture, relationships between components are inherently **directed** (e.g., `AuthService -[:USES]-> UserRepository`, `UserRepository -[:READS_FROM]-> Database`).

- **Original Neo4j Graph**: Stored strictly as directed edges. Never rewritten into undirected relationships or duplicate reverse edges.
- **GDS Projection**: Community detection requires structural connectivity regardless of invocation flow. The GDS projection configures `undirectedRelationshipTypes: $relationship_types` solely inside the temporary in-memory graph projection.
- **Context Summarization**: When loading context for summarization, relationships are loaded via directed traversal `(e)-[r]->(neighbor)` where both endpoints belong to the community. This ensures the LLM receives the exact, authoritative direction without duplicate reversed paths.

---

## 5. Expected Neo4j Graph Structure

```text
(:Repository)-[:HAS_COMMIT]->(:Commit)-[:CONTAINS]->(:File)-[:HAS_CHUNK]->(:Chunk)
                                                                 |
                                                             [:MENTIONS]
                                                                 v
(:Community {
    communityId: "comm_a4f810bc9123",
    entityCount: 3,
    architecturalRole: "User Authentication & Credential Verification",
    summary: "Manages credential validation, JWT token issuance, and user repository lookups.",
    keyEntities: ["AuthService", "UserRepository", "Database"],
    summaryHash: "a4f8...",
    contextHash: "c19b...",
    embeddedSummaryHash: "e721...",
    embedding: [0.012, -0.045, ...]
})
      ^                   ^                   ^
      | [:MEMBER_OF]      | [:MEMBER_OF]      | [:MEMBER_OF]
      |                   |                   |
(:Service:__Entity__ {id: "auth_svc", name: "AuthService", communityId: "comm_a4f810bc9123"})
      |
      | -[:USES]-> (:Repository:__Entity__ {id: "user_repo", name: "UserRepository", communityId: "comm_a4f810bc9123"})
                        |
                        | -[:READS_FROM]-> (:Database:__Entity__ {id: "db_main", name: "Database", communityId: "comm_a4f810bc9123"})
```

---

## 6. Change Detection & Recovery Invariant

The community layer decouples summary generation from embedding generation:

$$\text{Embedding is Current} \iff (\text{has\_embedding} = \text{True}) \land (\text{embeddedSummaryHash} = \text{currentVersionHash})$$

| Scenario | State | Action Taken |
| :--- | :--- | :--- |
| **NEW** | No summary, no embedding | Generate summary, persist summary, generate embedding, persist embedding. |
| **UNCHANGED** | Context matches, summary exists, embedding matches | Skip summary generation, skip embedding generation. |
| **MISSING EMBEDDING** | Context matches, summary exists, embedding missing | Skip summary generation, generate embedding, persist embedding. |
| **STALE EMBEDDING** | Context matches, summary exists, embedding hash mismatch | Skip summary generation, regenerate embedding, persist embedding. |
| **CHANGED CONTEXT** | Context hash differs | Regenerate summary, persist summary, regenerate embedding, persist embedding. |
| **FORCE REFRESH** | `force_refresh=True` | Regenerate summary and embedding for all targeted communities. |

---

## 7. Diagnostic Cypher Queries

Use these queries to inspect and verify the graph after running the community pipeline:

### 1. Community Count & Basic Stats
```cypher
MATCH (c:Community)
RETURN count(c) AS total_communities,
       avg(c.entityCount) AS avg_entities_per_community,
       min(c.entityCount) AS min_entities,
       max(c.entityCount) AS max_entities;
```

### 2. Verify Single Membership Invariant
```cypher
// Every entity MUST have count = 1. If any entity has count <> 1, an integrity issue exists.
MATCH (e:__Entity__)
OPTIONAL MATCH (e)-[:MEMBER_OF]->(c:Community)
WITH e, count(c) AS membership_count
WHERE membership_count <> 1
RETURN e.name AS entity_name, membership_count;
```

### 3. Community Membership Contents
```cypher
MATCH (e:__Entity__)-[:MEMBER_OF]->(c:Community)
RETURN c.communityId, c.architecturalRole, collect(e.name) AS members
ORDER BY size(members) DESC;
```

### 4. Verify Internal Directed Relationships
```cypher
// Confirms that relationships inside the community preserve true directed orientation
MATCH (a:__Entity__)-[r]->(b:__Entity__)
MATCH (a)-[:MEMBER_OF]->(c:Community)<-[:MEMBER_OF]-(b)
WHERE type(r) <> "MEMBER_OF"
RETURN c.communityId, a.name AS source, type(r) AS rel, b.name AS target
LIMIT 25;
```

### 5. Verify Embeddings & State Hashes
```cypher
MATCH (c:Community)
RETURN
    c.communityId,
    c.summary IS NOT NULL AS has_summary,
    c.embedding IS NOT NULL AS has_embedding,
    c.summaryHash = c.embeddedSummaryHash AS embedding_is_current,
    c.summaryHash,
    c.embeddedSummaryHash;
```

### 6. Verify Original Architectural Graph
```cypher
// Confirms that original architectural relationships remain intact
MATCH (s:__Entity__)-[r]->(t:__Entity__)
WHERE type(r) <> "MEMBER_OF"
RETURN type(r) AS relationship_type, count(r) AS count
ORDER BY count DESC;
```

---

## 8. Current Scope & Known Limitations

### Implemented Now
- In-memory GDS projection with dynamic relationship discovery (excluding `MEMBER_OF`).
- Leiden and Louvain community detection with temporary property isolation (`_gdsCommunityId`).
- Pluggable community identity strategies (`SnapshotCommunityIdStrategy`, `DeterministicCommunityIdStrategy`) with collision validation.
- Directed community context extraction excluding external cross-community edges.
- Structured LLM architectural summarization with strict entity validation and length constraints.
- Decoupled change detection recovering missing or stale embeddings without re-calling LLMs.
- Provider-agnostic batch embedding generation through the project's `Embedder` protocol.
- Neo4j vector index verification, creation, dimension validation, and readiness polling.
- Authoritative full synchronization vs safe targeted community processing.

### Not Implemented Yet (Future Roadmap)
- **Repository-diff-driven incremental community detection**: Graph topology is currently partitioned on the full projected snapshot; partial graph Leiden partitioning is a future research milestone.
- **Automatic affected-community detection**: Determining which communities are affected by a git commit diff requires integration with file-to-chunk provenance.
- **Cross-snapshot community lineage**: Tracking community evolution across git commits over time.
- **Hybrid GraphRAG retrieval application**: The downstream retrieval agent that queries `community_vector_index` alongside entity vector indexes.
