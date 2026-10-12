from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

from ai_services.ingestion.adr.applicability import ApplicabilityMetadata
from ai_services.ingestion.adr.enrichment import EnrichmentReport
from ai_services.ingestion.adr.preview import SavedPreviewReport
from ai_services.runs import run_adr_applicability as cli
from ai_services.tests.test_adr_applicability import enricher, scoped


async def saved_preview(service):
    report = await service.run("repo_a", preview=True, progress=lambda _: None)
    artifact = SavedPreviewReport.model_validate_json(json.dumps(asdict(report)))
    service.llm.extract_structured.reset_mock()
    return artifact


async def test_import_saves_reviewed_metadata_without_llm_or_provenance_changes():
    service = enricher()
    artifact = await saved_preview(service)
    original_nodes = deepcopy(service.store.nodes)
    original_provenance = deepcopy(service.store.provenance)
    dry_run = service.apply_preview("repo_a", artifact, progress=lambda _: None)
    assert dry_run.planned == 3 and dry_run.updated == 0 and dry_run.llm_calls == 0
    assert service.store.nodes == original_nodes
    applied = service.apply_preview(
        "repo_a", artifact, dry_run=False, progress=lambda _: None
    )
    assert applied.updated == 3 and applied.llm_calls == 0
    for entry in artifact.previews:
        node = service.store.nodes[entry.requirement_id]
        assert (
            ApplicabilityMetadata.from_json(node["applicability_json"])
            == entry.metadata
        )
        assert {
            k: node[k] for k in original_nodes[entry.requirement_id]
        } == original_nodes[entry.requirement_id]
        assert node["applicability_model"] == artifact.model_version
    assert service.store.provenance == original_provenance
    service.llm.extract_structured.assert_not_called()
    repeated = service.apply_preview(
        "repo_a", artifact, dry_run=False, progress=lambda _: None
    )
    assert repeated.skipped == 3 and repeated.updated == 0 and repeated.llm_calls == 0


async def test_legacy_preview_supported_without_inventing_source_history_or_model():
    service = enricher()
    artifact = await saved_preview(service)
    legacy = asdict(EnrichmentReport(planned=3, llm_calls=22))
    for field in ("repository_id", "model_version", "preview_version", "warnings"):
        legacy.pop(field)
    legacy["previews"] = [
        {"requirement_id": e.requirement_id, "metadata": e.metadata.model_dump()}
        for e in artifact.previews
    ]
    artifact = SavedPreviewReport.model_validate(legacy)
    service.model_version = "unknown"
    applied = service.apply_preview(
        "repo_a", artifact, dry_run=False, progress=lambda _: None
    )
    assert applied.updated == 3 and applied.llm_calls == 0
    assert "before this import cannot be detected" in applied.warnings[0]
    assert all(
        n["applicability_model"] == "legacy-preview-unknown"
        for n in service.store.nodes.values()
    )
    service.llm.extract_structured.assert_not_called()
    repeated = service.apply_preview(
        "repo_a", artifact, dry_run=False, progress=lambda _: None
    )
    assert repeated.skipped == 3 and repeated.updated == 0


@pytest.mark.parametrize(
    "change", ["requirement", "chunk", "document", "existing_enrichment"]
)
async def test_versioned_preview_rejects_source_changes_before_any_writes(change):
    service = enricher()
    artifact = await saved_preview(service)
    if change == "requirement":
        service.store.nodes["r2"]["description"] = "Changed requirement"
    elif change == "chunk":
        service.store.chunks[0].text += "Changed content"
    elif change == "document":
        service.store.doc.source_version = "new-source-version"
    else:
        service.store.nodes["r2"]["applicability_json"] = ApplicabilityMetadata(
            entity_ids=["someone-elses-result"],
        ).to_json()
    with pytest.raises(ValueError, match="Source changed since preview"):
        service.apply_preview(
            "repo_a", artifact, dry_run=False, progress=lambda _: None
        )
    assert not service.store.writes
    service.llm.extract_structured.assert_not_called()


@pytest.mark.parametrize(
    "change",
    ["repository", "unknown_id", "duplicate_id", "missing_result", "failed_preview"],
)
async def test_import_rejects_invalid_artifacts_before_writes(change):
    service = enricher()
    artifact = await saved_preview(service)
    if change == "repository":
        artifact.repository_id = "another-repository"
    elif change == "unknown_id":
        artifact.previews[-1].requirement_id = "unknown-id"
    elif change == "duplicate_id":
        artifact.previews[-1].requirement_id = artifact.previews[0].requirement_id
    elif change == "missing_result":
        artifact.previews.pop()
    else:
        artifact.failed = 1
    with pytest.raises(ValueError):
        service.apply_preview(
            "repo_a", artifact, dry_run=False, progress=lambda _: None
        )
    assert not service.store.writes
    service.llm.extract_structured.assert_not_called()


async def test_import_rejects_foreign_evidence_and_invented_resolved_ids():
    service = enricher()
    artifact = await saved_preview(service)
    artifact.previews[-1].metadata.applicability.evidence_chunk_ids.append(
        "foreign-adr-chunk"
    )
    with pytest.raises(ValueError, match="Invalid ADR evidence"):
        service.apply_preview(
            "repo_a", artifact, dry_run=False, progress=lambda _: None
        )
    assert not service.store.writes
    artifact = await saved_preview(service)
    artifact.previews[-1].metadata.entity_ids.append("fabricated-entity-id")
    with pytest.raises(ValueError, match="Graph associations changed"):
        service.apply_preview(
            "repo_a", artifact, dry_run=False, progress=lambda _: None
        )
    assert not service.store.writes


async def test_import_rejects_changed_associations_and_does_not_silently_reresolve():
    service = enricher()
    service.llm.extract_structured.side_effect = None
    # Capture entity metadata through the production preview path.
    from ai_services.ingestion.adr.enrichment import (
        EnrichmentResult,
        RequirementEnrichment,
    )

    service.llm.extract_structured.return_value = EnrichmentResult(
        requirements=[
            RequirementEnrichment(requirement_id=f"r{i}", applicability=scoped())
            for i in range(3)
        ]
    )
    artifact = await saved_preview(service)
    assert artifact.previews[0].metadata.entity_ids == ["payment-id"]
    service.resolver.load_graph.return_value = ({}, {})
    with pytest.raises(ValueError, match="Graph associations changed"):
        service.apply_preview(
            "repo_a", artifact, dry_run=False, progress=lambda _: None
        )
    assert not service.store.writes
    service.llm.extract_structured.assert_not_called()


async def test_import_resumes_after_interruption_without_extraction():
    service = enricher(batch_size=2)
    artifact = await saved_preview(service)
    update = service.store.update_batch
    calls = 0

    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("stop after a committed batch")
        return update(*args, **kwargs)

    service.store.update_batch = interrupt
    with pytest.raises(KeyboardInterrupt):
        service.apply_preview(
            "repo_a", artifact, dry_run=False, progress=lambda _: None
        )
    service.store.update_batch = update
    resumed = service.apply_preview(
        "repo_a", artifact, dry_run=False, progress=lambda _: None
    )
    assert resumed.skipped == 2 and resumed.updated == 1 and resumed.llm_calls == 0
    service.llm.extract_structured.assert_not_called()


async def test_import_write_conflict_is_reported_and_retryable():
    service = enricher()
    artifact = await saved_preview(service)
    update = service.store.update_batch
    service.store.update_batch = MagicMock(
        side_effect=RuntimeError("source changed during write")
    )
    report = service.apply_preview(
        "repo_a", artifact, dry_run=False, progress=lambda _: None
    )
    assert report.failed == 3 and report.updated == 0 and report.llm_calls == 0
    assert not service.store.writes
    service.store.update_batch = update
    assert (
        service.apply_preview(
            "repo_a", artifact, dry_run=False, progress=lambda _: None
        ).updated
        == 3
    )


async def test_cli_preview_import_needs_no_azure_client_or_settings(
    tmp_path, monkeypatch
):
    service = enricher()
    artifact = await saved_preview(service)
    source = tmp_path / "reviewed.json"
    output = tmp_path / "applied.json"
    source.write_text(artifact.model_dump_json(), encoding="utf-8")
    before = source.read_bytes()
    for name in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
        monkeypatch.setenv(name, "test-value")
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    driver = MagicMock()
    monkeypatch.setattr(cli.GraphDatabase, "driver", MagicMock(return_value=driver))
    azure = MagicMock(
        side_effect=AssertionError("import must not create an Azure client")
    )
    monkeypatch.setattr(cli, "AsyncAzureOpenAI", azure)
    importer = MagicMock()
    importer.apply_preview.return_value = EnrichmentReport(updated=3, llm_calls=0)
    factory = MagicMock(return_value=importer)
    monkeypatch.setattr(cli, "ADRApplicabilityEnricher", factory)
    result = await cli.main(
        [
            "--repository-id",
            "repo_a",
            "--from-preview",
            str(source),
            "--apply",
            "--report",
            str(output),
        ]
    )
    assert result == 0
    assert factory.call_args.args[2] is None
    azure.assert_not_called()
    importer.apply_preview.assert_called_once()
    importer.run.assert_not_called()
    assert importer.apply_preview.call_args.kwargs["dry_run"] is False
    assert source.read_bytes() == before
    assert json.loads(output.read_text())["llm_calls"] == 0
    driver.close.assert_called_once()


@pytest.mark.parametrize(
    "extra",
    [["--preview"], ["--force"], ["--refresh-associations"], ["--report", "same.json"]],
)
def test_cli_rejects_import_flag_conflicts_and_preview_overwrite(extra):
    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--repository-id", "repo_a", "--from-preview", "same.json", *extra]
        )
