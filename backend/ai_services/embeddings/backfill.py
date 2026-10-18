"""Resumable vector-only migration using an existing owned repository fence.

No extraction, node deletion, relationship changes or automatic provisioning.
Only use against a separately cloned SQL/Neo4j environment.
"""
import asyncio
import hashlib
import json
import math
import os
from dataclasses import dataclass
from shared.ai.space import assert_vector_space, vector_metadata


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class VectorSource:
    key: str
    text: str
    source_hash: str
    completed: bool


class VectorBackfill:
    def __init__(self, store, embedder):
        if getattr(embedder, "provider", None) != "google":
            raise ValueError("Google backfill requires a Google embedder")
        self.store, self.embedder = store, embedder

    async def run(self):
        sources = await asyncio.to_thread(self.store.sources)
        manifest = fingerprint([(s.key, s.source_hash) for s in sources])
        await asyncio.to_thread(self.store.begin, manifest, len(sources))
        completed = 0
        for source in sources:
            if not source.completed:
                if hasattr(self.store, "reserve"):
                    await asyncio.to_thread(self.store.reserve)
                vector = (await self.embedder.embed([source.text]))[0]
                # Store compares source and generation again under the owned fence.
                await asyncio.to_thread(self.store.save, source, vector)
            completed += 1
            await asyncio.to_thread(self.store.progress, manifest, completed)
        final_sources = await asyncio.to_thread(self.store.sources)
        if (fingerprint([(s.key, s.source_hash) for s in final_sources]) != manifest
                or not all(s.completed for s in final_sources)):
            raise ValueError("Vector sources changed during migration")
        await asyncio.to_thread(self.store.finish, manifest, completed)
        return {"expected": len(sources), "completed": completed, "manifest": manifest}


class Neo4jVectorBackfillStore:
    def __init__(self, fenced_driver, config):
        from ai_services.ingestion.graph_control import FencedDriver
        if not isinstance(fenced_driver, FencedDriver) or config.provider != "google":
            raise ValueError("Backfill requires a Google configuration and an owned repository fence")
        self.driver, self.config = fenced_driver, config
        self.repo = fenced_driver.run.repository_id
        self.space = config.embedding_space

    def _rows(self):
        return self.driver.execute_query("""
            MATCH (n {repository:$repo}) WHERE n:Chunk OR n:Entity OR n:Community
            RETURN elementId(n) AS key, labels(n) AS labels, properties(n) AS props
            ORDER BY key
            """, repo=self.repo).records

    @staticmethod
    def _source(row, space):
        p, labels = dict(row["props"]), row["labels"]
        # Use explicit deterministic, versioned templates for all searchable kinds.
        if "Chunk" in labels:
            text = p.get("text") or p.get("content")
        elif "Community" in labels:
            text = (f"Architectural Role: {p.get('architecturalRole', '').strip()}\n"
                    f"Summary: {p.get('summary', '').strip()}")
            if p.get("keyEntities"):
                text += "\nKey Entities: " + ", ".join(sorted(p["keyEntities"]))
        else:
            from types import SimpleNamespace
            from ai_services.ingestion.rkg.pipeline import RepositoryKnowledgePipeline
            text = RepositoryKnowledgePipeline._entity_embedding_text(SimpleNamespace(
                name=p.get("name", ""), label=p.get("label", ""), aliases=p.get("aliases", []),
                properties={k: v for k, v in p.items()
                    if k not in {"id", "name", "label", "aliases", "repository"}
                    and not k.startswith("embedding")}))
        if not text or not text.strip():
            raise ValueError("Searchable graph node has no source text")
        excluded = {"embedding", "embeddingSpace", "embeddingSourceHash", "embeddingTemplate",
            "embedding_provider", "embedding_model", "embedding_dimensions", "embedding_revision", "embedding_text_hash"}
        # Any source/provenance change invalidates the verified checkpoint.
        source_hash = fingerprint({k: v for k, v in p.items() if k not in excluded})
        vector = p.get("embedding") or []
        done = (p.get("embeddingSpace") == space and p.get("embeddingSourceHash") == source_hash
                and p.get("embeddingTemplate") == "backfill-text-v1"
                and len(vector) == 3072 and all(math.isfinite(v) for v in vector) and any(vector))
        return VectorSource(row["key"], text, source_hash, done)

    def sources(self):
        return [self._source(row, self.space) for row in self._rows()]

    def begin(self, manifest, expected):
        prior = self.driver.execute_query("""
            MATCH (m:VectorMigration {repository:$repo, space:$space})
            RETURN m.run_id AS run_id, m.token AS token, m.target AS target
            """, repo=self.repo, space=self.space).records
        run = self.driver.run
        if prior and prior[0]["run_id"] == run.id and (
                prior[0]["target"] != run.target_commit_sha or
                run.token not in {prior[0]["token"], prior[0]["token"] + 1}):
            raise ValueError("Backfill graph generation changed; a fresh migration run is required")
        self.driver.execute_query("""
            MERGE (m:VectorMigration {repository:$repo, space:$space})
            SET m.requests=CASE WHEN m.run_id=$run THEN coalesce(m.requests,0) ELSE 0 END
            SET m.manifest=$manifest, m.expected=$expected, m.completed=0,
                m.state='BUILDING', m.providerIdentity=$identity,
                m.run_id=$run, m.token=$token, m.target=$target
            """, repo=self.repo, space=self.space, manifest=manifest, expected=expected,
            identity=self.config.identity, run=run.id, token=run.token, target=run.target_commit_sha)

    def reserve(self):
        maximum = int(os.environ.get("GOOGLE_BACKFILL_MAX_REQUESTS", "10000"))
        if maximum < 1:
            raise ValueError("Backfill request ceiling must be positive")
        rows = self.driver.execute_query("""
            MATCH (m:VectorMigration {repository:$repo, space:$space})
            WHERE m.state='BUILDING' AND coalesce(m.requests,0)<$maximum
            SET m.requests=coalesce(m.requests,0)+1 RETURN m.requests AS requests
            """, repo=self.repo, space=self.space, maximum=maximum).records
        if len(rows) != 1:
            raise ValueError("Backfill durable request budget exhausted")

    def save(self, source, vector):
        import math
        if len(vector) != 3072 or not all(math.isfinite(v) for v in vector) or not any(vector):
            raise ValueError("Backfill vector is invalid")
        # Source comparison and vector write share the graph ownership transaction.
        with self.driver.control.transaction(self.driver.run) as tx:
            rows = list(tx.run("MATCH (n) WHERE elementId(n)=$key AND n.repository=$repo "
                "RETURN elementId(n) AS key, labels(n) AS labels, properties(n) AS props",
                key=source.key, repo=self.repo))
            if len(rows) != 1 or self._source(rows[0], self.space).source_hash != source.source_hash:
                raise ValueError("Backfill source changed")
            list(tx.run("MATCH (n) WHERE elementId(n)=$key "
                "SET n.embedding=$vector, n.embeddingSpace=$space, "
                "n.embeddingSourceHash=$hash, n.embeddingTemplate='backfill-text-v1' SET n += $metadata",
                key=source.key, vector=vector, space=self.space, hash=source.source_hash,
                metadata=vector_metadata(source.text)))

    def progress(self, manifest, completed):
        self.driver.execute_query("""
            MATCH (m:VectorMigration {repository:$repo, space:$space, manifest:$manifest})
            SET m.completed=$completed
            """, repo=self.repo, space=self.space, manifest=manifest, completed=completed)

    def finish(self, manifest, completed):
        assert_vector_space(self.driver, self.driver.control.database, self.repo)
        self.driver.execute_query("""
            MATCH (m:VectorMigration {repository:$repo, space:$space, manifest:$manifest})
            WHERE m.expected=$completed AND m.completed=$completed
            SET m.state='READY'
            """, repo=self.repo, space=self.space, manifest=manifest, completed=completed)
