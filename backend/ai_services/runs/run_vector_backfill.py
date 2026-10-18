"""Explicitly authorized vector-only migration in an isolated Google data clone.

This command queues an ordinary fenced repository Job with trigger=vector_backfill.
It does not dispatch, provision resources, execute models or alter source records.
The existing repository worker handles the resumable vector-only branch.
"""
import argparse
from shared.ai.provider import ProviderConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--authorize-paid-backfill", action="store_true")
    args = parser.parse_args()
    if not args.authorize_paid_backfill:
        parser.error("Queueing requires explicit approval for subsequent billable embedding requests")
    if ProviderConfig.from_env().provider != "google":
        parser.error("Vector migration requires the isolated Google configuration")
    from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
    from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs
    store = SqliteApplicationStore()
    repo = store.get_repository(args.repository_id)
    if not repo or repo.indexed_commit_sha != args.expected_sha:
        raise ValueError("Repository baseline changed")
    # Reuse existing auth/lease/epoch/deletion controls at worker execution.
    run_id = IngestionJobs(store.session_factory).enqueue(repo.id, args.expected_sha,
        branch=repo.tracked_branch, installation_id=repo.installation_id, trigger="vector_backfill")
    print(f"Queued vector-only run {run_id}; expected/completed counts persist in Neo4j VectorMigration")


if __name__ == "__main__":
    main()
