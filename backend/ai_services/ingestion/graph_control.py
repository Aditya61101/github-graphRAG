"""Repository fence is checked under the same Neo4j lock as every graph mutation."""
from contextlib import contextmanager
from neo4j import EagerResult
from .persistence.ingestion_jobs import LostIngestionLease


def require_fenced_writer(driver):
    """Production maintenance/CLI paths must not bypass API ownership either."""
    import os
    remote = (os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB")
              or os.getenv("APP_ENV", "").lower() == "production"
              or os.getenv("DATABASE_URL", "").startswith("postgresql"))
    if remote and not isinstance(driver, FencedDriver):
        raise RuntimeError("Production graph mutation requires owned repository ingestion")


class GraphControl:
    def __init__(self, driver, database="neo4j"):
        self.driver, self.database = driver, database

    def initialize(self):
        self.driver.execute_query(
            "CREATE CONSTRAINT ingestion_graph_repository IF NOT EXISTS "
            "FOR (n:RepositoryGraphState) REQUIRE n.repository_id IS UNIQUE",
            database_=self.database)

    @contextmanager
    def _locked(self, repo):
        with self.driver.session(database=self.database) as session:
            with session.begin_transaction(timeout=120) as tx:
                # Dependency on the existing property forces the write lock first.
                list(tx.run("MATCH (g:RepositoryGraphState {repository_id:$repo}) "
                    "SET g.lock_serial = coalesce(g.lock_serial,0)+1 RETURN g.lock_serial",
                    repo=repo))
                yield tx
                if not tx.closed():
                    tx.commit()

    def arm(self, run):
        with self.driver.session(database=self.database) as session:
            with session.begin_transaction(timeout=120) as tx:
                list(tx.run("MERGE (g:RepositoryGraphState {repository_id:$repo}) "
                    "ON CREATE SET g.token=0, g.lock_serial=0 "
                    "SET g.lock_serial=g.lock_serial+1", repo=run.repository_id))
                rows = list(tx.run("""
                    MATCH (g:RepositoryGraphState {repository_id:$repo})
                    WHERE g.token < $token
                    SET g.token=$token, g.owner=$owner, g.run_id=$run,
                        g.state='BUILDING', g.lease_until=$lease, g.target=$target
                    RETURN g.token
                    """, repo=run.repository_id, token=run.token, owner=run.owner,
                    run=run.id, lease=run.lease_until, target=run.target_commit_sha))
                if len(rows) != 1:
                    raise LostIngestionLease("Graph fence is newer or already sealed")
                tx.commit()

    @contextmanager
    def transaction(self, run):
        with self._locked(run.repository_id) as tx:
            rows = list(tx.run("""
                MATCH (g:RepositoryGraphState {repository_id:$repo})
                WHERE g.token=$token AND g.owner=$owner AND g.run_id=$run
                  AND g.state='BUILDING' AND g.lease_until > timestamp()/1000
                RETURN g.token
                """, repo=run.repository_id, token=run.token, owner=run.owner, run=run.id))
            if len(rows) != 1:
                raise LostIngestionLease("Graph ownership expired or superseded")
            yield tx

    def renew(self, run, lease):
        with self.transaction(run) as tx:
            list(tx.run("MATCH (g:RepositoryGraphState {repository_id:$repo}) "
                        "SET g.lease_until=$lease", repo=run.repository_id, lease=lease))

    def seal(self, run):
        with self.transaction(run) as tx:
            list(tx.run("MATCH (g:RepositoryGraphState {repository_id:$repo}) "
                        "SET g.state='COMPLETE'", repo=run.repository_id))

    def snapshot(self, repo):
        rows = self.driver.execute_query(
            "MATCH (g:RepositoryGraphState {repository_id:$repo}) "
            "RETURN g.token AS token, g.run_id AS run_id, g.target AS target, g.state AS state",
            repo=repo, database_=self.database).records
        return dict(rows[0]) if rows else None


class FencedDriver:
    """Small driver adapter: existing code/community queries share the graph fence."""
    def __init__(self, control, run):
        self.control, self.run = control, run

    @contextmanager
    def session(self, **_):
        driver = self
        class Session:
            def begin_transaction(self, **_kwargs):
                return driver.control.transaction(driver.run)
        yield Session()

    def execute_query(self, query, **params):
        params.pop("database_", None)
        params.pop("routing_", None)
        # Schema provisioning is explicit, before workers; verify/create indexes
        # through the underlying driver because Neo4j forbids mixed schema/data tx.
        if query.lstrip().upper().startswith(("CREATE CONSTRAINT", "CREATE INDEX", "CREATE VECTOR INDEX")):
            return self.control.driver.execute_query(query, database_=self.control.database, **params)
        with self.control.transaction(self.run) as tx:
            result = tx.run(query, **params)
            records, keys = list(result), list(result.keys())
            summary = result.consume()
            return EagerResult(records, summary, keys)
