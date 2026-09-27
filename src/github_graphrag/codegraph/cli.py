"""
Command line entry point.

    uv run codegraph ingest ../claimIQ --repo claimIQ     # first run AND updates
    uv run codegraph ask "How does login work?" --repo claimIQ
    uv run codegraph impact verify_password --repo claimIQ
    uv run codegraph status --repo claimIQ
    uv run codegraph calibrate --repo claimIQ            # pick BRIDGE_MIN_SCORE
    uv run codegraph delete-repo claimIQ
    uv run codegraph reset-embeddings                     # after switching embedding model
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codegraph", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="index a repo (new or update)")
    p.add_argument("path", type=Path)
    p.add_argument("--repo", help="name in the graph (default: folder name)")
    p.add_argument("--verify-links", action="store_true",
                   help="ask the LLM to confirm every doc->code vector link")

    p = sub.add_parser("ask", help="ask a question")
    p.add_argument("question")
    p.add_argument("--repo", required=True)
    p.add_argument("--show-context", action="store_true")

    p = sub.add_parser("impact", help="what breaks if this function changes")
    p.add_argument("name")
    p.add_argument("--repo", required=True)
    p.add_argument("--depth", type=int, default=3)

    p = sub.add_parser("status", help="counts for a repo")
    p.add_argument("--repo", required=True)

    p = sub.add_parser("calibrate", help="print doc->code similarity pairs to pick a threshold")
    p.add_argument("--repo", required=True)
    p.add_argument("--n", type=int, default=20)

    p = sub.add_parser("delete-repo", help="remove a repo from the graph")
    p.add_argument("repo")

    sub.add_parser("reset-embeddings", help="drop vectors after changing embedding model")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")
    for noisy in ("neo4j", "httpx", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from dotenv import load_dotenv
    load_dotenv()

    from .config import Settings, build_driver, build_embedder, build_llm
    from .store import GraphStore

    settings = Settings()
    driver = build_driver()
    store = GraphStore(driver, settings.neo4j_database)
    try:
        return asyncio.run(_run(args, settings, store, build_llm, build_embedder))
    finally:
        driver.close()


async def _run(args, settings, store, build_llm, build_embedder) -> int:
    from .retrieve import ask, format_impact, impact
    from .sync import delete_repo

    if args.command == "ingest":
        from .pipeline import ingest
        repo = args.repo or args.path.resolve().name
        report = await ingest(store, build_llm(settings), build_embedder(settings),
                              args.path, repo, settings, verify_links=args.verify_links)
        print(json.dumps(report, indent=2, default=str))

    elif args.command == "ask":
        result = await ask(store, build_llm(settings), build_embedder(settings),
                           args.repo, args.question)
        if args.show_context or result.answer is None:
            print(result.context)
            print("\n" + "-" * 60)
        if result.answer:
            print(result.answer)
        if result.sources:
            print("\nSources:\n" + "\n".join(f"  - {s}" for s in result.sources))

    elif args.command == "impact":
        print(format_impact(args.name, impact(store, args.repo, args.name, args.depth)))

    elif args.command == "status":
        print(json.dumps(status(store, args.repo), indent=2))

    elif args.command == "calibrate":
        calibrate(store, args.repo, args.n)

    elif args.command == "delete-repo":
        print(f"deleted {delete_repo(store, args.repo)} nodes")

    elif args.command == "reset-embeddings":
        store.drop_vector_indexes()
        for label in ("Function", "Section", "Module"):
            store.run(f"MATCH (n:{label}) REMOVE n.embedding")
        store.run("MATCH (m:Module) REMOVE m.members_hash")
        print("embeddings removed; run `codegraph ingest` again")
    return 0


def status(store, repo: str) -> dict:
    out = {}
    repo_row = store.run("MATCH (r:Repo {name: $repo}) RETURN r.indexed_commit AS commit, "
                         "r.status AS status", repo=repo)
    out["repo"] = repo_row[0] if repo_row else None
    for label in ("File", "Class", "Function", "Endpoint", "Doc", "Section", "Module"):
        out[label] = store.run(f"MATCH (n:{label} {{repo: $repo}}) RETURN count(n) AS total",
                               repo=repo)[0]["total"]
    out["summarised"] = store.run(
        "MATCH (f:Function {repo: $repo}) WHERE f.summary IS NOT NULL RETURN count(f) AS total",
        repo=repo)[0]["total"]
    for rel in ("CALLS", "IMPORTS", "INHERITS", "MENTIONS", "RELATES_TO"):
        out[rel] = store.run(
            f"MATCH (a)-[r:{rel}]->(b) WHERE a.repo = $repo RETURN count(r) AS total",
            repo=repo)[0]["total"]
    out["doc_links_may_be_outdated"] = store.run(
        "MATCH (s:Section {repo: $repo})-[r]->() WHERE r.doc_may_be_outdated = true "
        "RETURN count(r) AS total", repo=repo)[0]["total"]
    return out


def calibrate(store, repo: str, n: int) -> None:
    """Show each section's best-matching functions with scores, to choose BRIDGE_MIN_SCORE."""
    sections = store.run(
        "MATCH (s:Section {repo: $repo}) WHERE s.embedding IS NOT NULL "
        "RETURN s.id AS id, s.heading AS heading, s.embedding AS e LIMIT $n", repo=repo, n=n)
    for s in sections:
        print(f"\n{s['heading']}")
        for hit in store.vector_search("Function", s["e"], 3, repo):
            print(f"   {hit['score']:.3f}  {hit['id'].split(':', 2)[-1]}")
    print("\nMark which pairs are correct; set BRIDGE_MIN_SCORE just below the lowest "
          "correct score.")


if __name__ == "__main__":
    sys.exit(main())
