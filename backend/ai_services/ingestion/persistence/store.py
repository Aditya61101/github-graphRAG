from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from neo4j import Driver

from ai_services.ingestion.sources.interface import RepositoryMetadata


@dataclass
class RepositoryRecord:
    """Persistent representation of a tracked repository."""
    github_repository_id: str | int
    owner: str
    name: str
    full_name: str
    default_branch: str
    tracked_branch: str
    repository_url: str
    visibility: str = "public"
    indexed_commit_sha: str | None = None
    status: str = "IDLE"  # "IDLE", "INDEXING", "COMPLETED", "FAILED"
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Neo4jRepositoryStore:
    """Manages repository registration and ingestion state directly in Neo4j."""

    def __init__(self, driver: Driver, database: str | None = None) -> None:
        self.driver = driver
        self.database = database
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        queries = [
            "CREATE CONSTRAINT repository_key IF NOT EXISTS FOR (r:Repository) REQUIRE r.key IS UNIQUE",
            "CREATE CONSTRAINT ingestion_run_id IF NOT EXISTS FOR (run:IngestionRun) REQUIRE run.id IS UNIQUE",
        ]
        for query in queries:
            try:
                self.driver.execute_query(query, database_=self.database)
            except Exception:
                pass

    def save_repository(self, metadata: RepositoryMetadata) -> RepositoryRecord:
        """Register or update a repository record."""
        result = self.driver.execute_query(
            """
            MERGE (r:Repository {key: $full_name})
            ON CREATE SET
                r.github_repository_id = $github_repo_id,
                r.owner = $owner,
                r.name = $name,
                r.full_name = $full_name,
                r.default_branch = $default_branch,
                r.tracked_branch = $tracked_branch,
                r.repository_url = $repository_url,
                r.visibility = $visibility,
                r.status = 'IDLE',
                r.created_at = $now
            ON MATCH SET
                r.default_branch = $default_branch,
                r.tracked_branch = $tracked_branch,
                r.repository_url = $repository_url,
                r.visibility = $visibility,
                r.updated_at = $now
            RETURN
                r.github_repository_id AS github_repository_id,
                r.owner AS owner,
                r.name AS name,
                r.full_name AS full_name,
                r.default_branch AS default_branch,
                r.tracked_branch AS tracked_branch,
                r.repository_url AS repository_url,
                r.visibility AS visibility,
                r.indexed_commit_sha AS indexed_commit_sha,
                r.status AS status,
                r.started_at AS started_at,
                r.completed_at AS completed_at,
                r.error AS error
            """,
            github_repo_id=str(metadata.github_repository_id),
            owner=metadata.owner,
            name=metadata.name,
            full_name=metadata.full_name,
            default_branch=metadata.default_branch,
            tracked_branch=metadata.tracked_branch,
            repository_url=metadata.repository_url,
            visibility=metadata.visibility,
            now=datetime.now(timezone.utc).isoformat(),
            database_=self.database,
        )

        record = result.records[0]
        return RepositoryRecord(
            github_repository_id=record["github_repository_id"],
            owner=record["owner"],
            name=record["name"],
            full_name=record["full_name"],
            default_branch=record["default_branch"],
            tracked_branch=record["tracked_branch"],
            repository_url=record["repository_url"],
            visibility=record["visibility"] or "public",
            indexed_commit_sha=record["indexed_commit_sha"],
            status=record["status"] or "IDLE",
            started_at=record["started_at"],
            completed_at=record["completed_at"],
            error=record["error"],
        )

    def get_repository(self, full_name: str) -> RepositoryRecord | None:
        result = self.driver.execute_query(
            """
            MATCH (r:Repository {key: $full_name})
            RETURN
                r.github_repository_id AS github_repository_id,
                r.owner AS owner,
                r.name AS name,
                r.full_name AS full_name,
                r.default_branch AS default_branch,
                r.tracked_branch AS tracked_branch,
                r.repository_url AS repository_url,
                r.visibility AS visibility,
                r.indexed_commit_sha AS indexed_commit_sha,
                r.status AS status,
                r.started_at AS started_at,
                r.completed_at AS completed_at,
                r.error AS error
            """,
            full_name=full_name,
            database_=self.database,
        )

        if not result.records:
            return None

        record = result.records[0]
        return RepositoryRecord(
            github_repository_id=record["github_repository_id"] or "",
            owner=record["owner"] or full_name.split("/")[0],
            name=record["name"] or full_name.split("/")[-1],
            full_name=record["full_name"] or full_name,
            default_branch=record["default_branch"] or "main",
            tracked_branch=record["tracked_branch"] or "main",
            repository_url=record["repository_url"] or f"https://github.com/{full_name}",
            visibility=record["visibility"] or "public",
            indexed_commit_sha=record["indexed_commit_sha"],
            status=record["status"] or "IDLE",
            started_at=record["started_at"],
            completed_at=record["completed_at"],
            error=record["error"],
        )

    def list_repositories(self) -> list[RepositoryRecord]:
        result = self.driver.execute_query(
            """
            MATCH (r:Repository)
            WHERE r.full_name IS NOT NULL
            RETURN
                r.github_repository_id AS github_repository_id,
                r.owner AS owner,
                r.name AS name,
                r.full_name AS full_name,
                r.default_branch AS default_branch,
                r.tracked_branch AS tracked_branch,
                r.repository_url AS repository_url,
                r.visibility AS visibility,
                r.indexed_commit_sha AS indexed_commit_sha,
                r.status AS status,
                r.started_at AS started_at,
                r.completed_at AS completed_at,
                r.error AS error
            ORDER BY r.full_name
            """,
            database_=self.database,
        )

        return [
            RepositoryRecord(
                github_repository_id=record["github_repository_id"] or "",
                owner=record["owner"] or "",
                name=record["name"] or "",
                full_name=record["full_name"],
                default_branch=record["default_branch"] or "main",
                tracked_branch=record["tracked_branch"] or "main",
                repository_url=record["repository_url"] or f"https://github.com/{record['full_name']}",
                visibility=record["visibility"] or "public",
                indexed_commit_sha=record["indexed_commit_sha"],
                status=record["status"] or "IDLE",
                started_at=record["started_at"],
                completed_at=record["completed_at"],
                error=record["error"],
            )
            for record in result.records
        ]

    def record_ingestion_start(
        self,
        full_name: str,
        commit_sha: str,
        is_incremental: bool = False,
    ) -> str:
        """Mark ingestion as in-progress and create an IngestionRun audit node."""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        self.driver.execute_query(
            """
            MERGE (r:Repository {key: $full_name})
            SET r.status = 'INDEXING',
                r.started_at = $now,
                r.error = NULL
            CREATE (run:IngestionRun {
                id: $run_id,
                commit_sha: $commit_sha,
                is_incremental: $is_incremental,
                status: 'INDEXING',
                started_at: $now
            })
            MERGE (r)-[:HAS_INGESTION_RUN]->(run)
            """,
            full_name=full_name,
            run_id=run_id,
            commit_sha=commit_sha,
            is_incremental=is_incremental,
            now=now,
            database_=self.database,
        )
        return run_id

    def record_ingestion_success(
        self,
        full_name: str,
        run_id: str,
        commit_sha: str,
    ) -> None:
        """Atomically advance indexed_commit_sha and mark status as COMPLETED."""
        now = datetime.now(timezone.utc).isoformat()

        self.driver.execute_query(
            """
            MATCH (r:Repository {key: $full_name})
            SET r.status = 'COMPLETED',
                r.indexed_commit_sha = $commit_sha,
                r.completed_at = $now,
                r.error = NULL
            WITH r
            OPTIONAL MATCH (r)-[:HAS_INGESTION_RUN]->(run:IngestionRun {id: $run_id})
            WHERE run IS NOT NULL
            SET run.status = 'COMPLETED',
                run.completed_at = $now
            """,
            full_name=full_name,
            run_id=run_id,
            commit_sha=commit_sha,
            now=now,
            database_=self.database,
        )

    def record_ingestion_failure(
        self,
        full_name: str,
        run_id: str,
        error: str,
    ) -> None:
        """Record ingestion failure without modifying indexed_commit_sha."""
        now = datetime.now(timezone.utc).isoformat()

        self.driver.execute_query(
            """
            MATCH (r:Repository {key: $full_name})
            SET r.status = 'FAILED',
                r.error = $error,
                r.completed_at = $now
            WITH r
            OPTIONAL MATCH (r)-[:HAS_INGESTION_RUN]->(run:IngestionRun {id: $run_id})
            WHERE run IS NOT NULL
            SET run.status = 'FAILED',
                run.error = $error,
                run.completed_at = $now
            """,
            full_name=full_name,
            run_id=run_id,
            error=str(error),
            now=now,
            database_=self.database,
        )
