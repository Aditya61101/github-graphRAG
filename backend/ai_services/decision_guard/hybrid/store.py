"""Immutable derived snapshots in the existing Neo4j database, not ingestion."""

from .models import RetrievalSettings, SearchIndex


class Neo4jSearchIndexStore:
    """Explicit snapshot-key reads; never query a global top-K then filter repo.

    No authoritative labels/properties/relationships are written. Snapshot mode
    and manifest identity must still be validated by the caller's lease/readiness.
    """

    def __init__(self, driver, database):
        self.driver, self.database = driver, database

    def initialize(self):
        self.driver.execute_query(
            "CREATE CONSTRAINT dg_search_snapshot_key IF NOT EXISTS FOR (n:DGRetrievalIndex) REQUIRE n.key IS UNIQUE",
            database_=self.database,
        )

    def save(self, index):
        index = SearchIndex.model_validate_json(index.model_dump_json())
        payload = index.model_dump_json()
        # One immutable snapshot makes publication atomic; bound before DB writes.
        if len(payload.encode("utf-8")) > RetrievalSettings().max_index_bytes:
            raise ValueError("Derived snapshot exceeds index storage/read bound")
        query = """
        MERGE (n:DGRetrievalIndex {key: $key})
        ON CREATE SET n.repository = $repo, n.base_sha = $sha,
          n.generation = $generation, n.run_id = $run_id, n.catalog_hash = $catalog_hash,
          n.schema_version = $schema_version, n.unit_count = $count,
          n.chunk_count = $chunk_count,
          n.payload_bytes = $bytes, n.payload_json = $payload, n.state = 'READY'
        RETURN n.repository AS repository, n.key AS key, n.state AS state
        """
        result = self.driver.execute_query(
            query,
            database_=self.database,
            key=index.index_id,
            repo=index.baseline.repository_id,
            sha=index.baseline.indexed_rkg_sha,
            generation=index.baseline.graph_generation,
            run_id=index.baseline.ingestion_run_id,
            catalog_hash=index.catalog_hash,
            schema_version=index.schema_version,
            count=len(index.units),
            chunk_count=len(index.adr_chunks),
            bytes=len(payload.encode("utf-8")),
            payload=payload,
        )
        records = getattr(result, "records", result)
        if (
            len(records) != 1
            or records[0]["repository"] != index.baseline.repository_id
            or records[0]["key"] != index.index_id
            or records[0]["state"] != "READY"
        ):
            raise ValueError("Derived index publication identity mismatch")

    def load(self, index_id, baseline, settings=None):
        settings = settings or RetrievalSettings()
        query = """
        MATCH (n:DGRetrievalIndex {key: $key, repository: $repo,
          base_sha: $sha, generation: $generation, run_id: $run_id})
        WHERE n.state = 'READY' AND n.unit_count <= $max_units AND n.chunk_count <= $max_chunks AND n.payload_bytes <= $max_bytes
        RETURN n.payload_json AS payload LIMIT 1
        """
        with (
            self.driver.session(database=self.database) as session,
            session.begin_transaction(timeout=settings.deadline_seconds) as tx,
        ):
            records = list(
                tx.run(
                    query,
                    key=index_id,
                    repo=baseline.repository_id,
                    sha=baseline.indexed_rkg_sha,
                    generation=baseline.graph_generation,
                    run_id=baseline.ingestion_run_id,
                    max_units=settings.max_index_units,
                    max_chunks=settings.max_semantic_chunks,
                    max_bytes=settings.max_index_bytes,
                )
            )
        if not records:
            raise ValueError(
                "Index missing, incompatible, incomplete or outside read budget"
            )
        index = SearchIndex.model_validate_json(records[0]["payload"])
        if (
            len(index.units) > settings.max_index_units
            or len(index.adr_chunks) > settings.max_semantic_chunks
            or len(records[0]["payload"].encode("utf-8")) > settings.max_index_bytes
        ):
            raise ValueError("Derived snapshot exceeds index read budget")
        if index.index_id != index_id or index.baseline != baseline:
            raise ValueError("Derived snapshot repository/generation mismatch")
        return index
