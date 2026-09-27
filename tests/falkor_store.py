"""
Test double for GraphStore backed by FalkorDB Lite (an embedded Cypher
database, pip install falkordblite, Python >= 3.12).

All pipeline Cypher runs unchanged on FalkorDB. Only the Neo4j-specific
parts are replaced:
  - constraints        -> plain indexes
  - vector indexes     -> skipped
  - vector search      -> cosine similarity computed in Python
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from github_graphrag.codegraph.store import GraphStore


class _Driver:
    def __init__(self, graph):
        self.graph = graph

    def execute_query(self, query, parameters_=None, database_=None, **kwargs):
        params = dict(parameters_ or {}, **kwargs)
        res = self.graph.query(query, params)
        header = [h[1] if isinstance(h, (list, tuple)) else h for h in (res.header or [])]
        records = [dict(zip(header, row)) for row in (res.result_set or [])]
        return records, None, None


class FalkorStore(GraphStore):
    def __init__(self, path: str | None = None, graph: str = "codegraph_test"):
        from redislite.falkordb_client import FalkorDB

        path = path or str(Path(tempfile.mkdtemp()) / "graph.rdb")
        self._db = FalkorDB(path)
        self._graph = self._db.select_graph(graph)
        super().__init__(_Driver(self._graph))

    def ensure_schema(self) -> None:
        for label, prop in [("File", "id"), ("Function", "id"), ("Function", "name"),
                            ("Class", "id"), ("Section", "id"), ("Doc", "id"),
                            ("Module", "id"), ("SummaryCache", "key"), ("Repo", "name")]:
            try:
                self._graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception:
                pass  # already exists

    def ensure_vector_indexes(self, dims: int, embedder_name: str) -> None:
        self.run(
            "MERGE (m:GraphMeta {key: 'embedding'}) SET m.model = $model, m.dims = $dims",
            model=embedder_name, dims=dims,
        )

    def drop_vector_indexes(self) -> None:
        self.run("MATCH (m:GraphMeta {key: 'embedding'}) DELETE m")

    def vector_search(self, label, vector, k, repo=None):
        rows = self.run(
            f"MATCH (n:{label}) WHERE n.embedding IS NOT NULL "
            "AND ($repo IS NULL OR n.repo = $repo) RETURN n.id AS id, n.embedding AS e",
            repo=repo,
        )
        scored = [{"id": r["id"], "score": _cosine_score(vector, r["e"])} for r in rows]
        return sorted(scored, key=lambda r: -r["score"])[:k]

    def dump(self) -> None:
        for row in self.run("MATCH (a)-[r]->(b) RETURN a.id AS a, type(r) AS t, b.id AS b "
                            "ORDER BY a, t, b"):
            print(f"  {row['a']} -[{row['t']}]-> {row['b']}")


def _cosine_score(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    # Neo4j reports cosine similarity normalised to [0, 1]
    return (1 + dot / (na * nb)) / 2
