"""Plan, preview, apply, or refresh repository-scoped ADR applicability enrichment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

from ai_services.ingestion.adr.applicability_store import ADRApplicabilityStore
from ai_services.ingestion.adr.enrichment import ADRApplicabilityEnricher
from ai_services.ingestion.adr.preview import SavedPreviewReport
from ai_services.ingestion.adr.scope_resolver import ADRScopeResolver
from shared.ai.provider import create_provider, ProviderConfig


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value.strip("'\"")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-id", required=True, help="Canonical repository ID, not owner/name"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only plan (default)")
    mode.add_argument(
        "--apply", action="store_true", help="Persist applicability-only updates"
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Call LLM and report outputs without writes",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-enrich current-version nodes"
    )
    parser.add_argument(
        "--refresh-associations",
        action="store_true",
        help="Refresh graph associations without LLM calls",
    )
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument(
        "--context-max-chars",
        type=int,
        default=int(os.getenv("ADR_DOCUMENT_CONTEXT_MAX_CHARS", "6000")),
    )
    parser.add_argument("--input-max-chars", type=int, default=24000)
    parser.add_argument(
        "--model-version", help="Deployment/revision label used for resumability"
    )
    parser.add_argument(
        "--from-preview",
        type=Path,
        help="Validate/import a reviewed preview JSON with zero LLM calls; combine with --apply to save",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional local JSON report, including preview outputs",
    )
    args = parser.parse_args(argv)
    if args.from_preview and (args.preview or args.force or args.refresh_associations):
        parser.error(
            "--from-preview cannot be combined with extraction, force, or association refresh"
        )
    if (
        args.from_preview
        and args.report
        and args.from_preview.resolve() == args.report.resolve()
    ):
        parser.error("The output report must not overwrite the reviewed preview file")
    if args.preview and (args.apply or args.refresh_associations):
        parser.error(
            "--preview requires dry-run extraction and cannot be combined with --apply or --refresh-associations"
        )
    return args


async def main(argv=None) -> int:
    load_dotenv()
    args = parse_args(argv)
    artifact = None
    if args.from_preview:
        artifact = SavedPreviewReport.model_validate_json(
            args.from_preview.read_text(encoding="utf-8-sig")
        )
        artifact.validate_for_import(args.repository_id)
    database = os.getenv("NEO4J_DATABASE") or "neo4j"
    driver = GraphDatabase.driver(
        required_env("NEO4J_URI"),
        auth=(
            required_env("NEO4J_USERNAME"),
            required_env("NEO4J_PASSWORD"),
        ),
    )
    services = None
    try:
        driver.verify_connectivity()
        deployment = ProviderConfig.from_env().model
        llm = None
        if (
            (args.apply or args.preview)
            and not args.refresh_associations
            and not args.from_preview
        ):
            services = create_provider()
            llm = services.llm
        enricher = ADRApplicabilityEnricher(
            ADRApplicabilityStore(driver, database),
            ADRScopeResolver(driver, database),
            llm,
            batch_size=args.batch_size,
            context_max_chars=args.context_max_chars,
            input_max_chars=args.input_max_chars,
            model_version=args.model_version
            or (deployment if not args.from_preview else None),
        )
        if artifact is not None:
            report = enricher.apply_preview(
                args.repository_id, artifact, dry_run=not args.apply
            )
        else:
            report = await enricher.run(
                args.repository_id,
                dry_run=not args.apply,
                preview=args.preview,
                force=args.force,
                refresh_associations=args.refresh_associations,
            )
        output = json.dumps(asdict(report), indent=2, ensure_ascii=False)
        print(output)
        if args.report:
            args.report.write_text(output + "\n", encoding="utf-8")
        return 1 if report.failed else 0
    finally:
        if services:
            await services.aclose()
        driver.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
