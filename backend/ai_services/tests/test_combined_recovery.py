"""Both durable queues recover independently through the private scheduler target."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ai_services.runs import evaluation_service as service


@pytest.mark.parametrize("failure", [None, "evaluations", "ingestion"])
async def test_combined_recovery_attempts_both_and_reports_partial_failure(monkeypatch, failure):
    monkeypatch.setattr(service, "require_enabled", lambda: None)
    store = object()
    monkeypatch.setattr(service, "SqliteApplicationStore", lambda: store)
    handlers = {}
    for component in ("evaluations", "ingestion"):
        handler = AsyncMock(side_effect=RuntimeError("private provider details") if component == failure else None)
        handlers[component] = handler
        name = "reconcile_evaluations" if component == "evaluations" else "reconcile_repository_ingestion"
        monkeypatch.setattr(service, name, handler)
    # An occupied evaluation slot must not prevent scheduler reconciliation.
    monkeypatch.setattr(service, "slot", asyncio.Semaphore(0))
    monkeypatch.setattr(service, "recovery_slot", asyncio.Semaphore(1))
    result = await asyncio.wait_for(service.reconcile(), timeout=5)
    for handler in handlers.values():
        handler.assert_awaited_once()
        assert handler.await_args.args[0] is store
    if failure:
        assert result.status_code == 503
        assert failure.encode() in result.body
        assert b"private provider details" not in result.body
    else:
        assert result == {"status": "reconciled", "components": ["evaluations", "ingestion"]}


async def test_repository_recovery_uses_direct_jobs_dispatcher(monkeypatch):
    store = SimpleNamespace(session_factory=object())
    jobs, dispatcher, http = object(), object(), object()
    monkeypatch.setattr(service, "IngestionJobs", Mock(return_value=jobs))
    monkeypatch.setattr(service, "configured_ingestion_dispatcher", Mock(return_value=dispatcher))
    reconcile = AsyncMock()
    monkeypatch.setattr(service, "reconcile_ingestion", reconcile)
    monkeypatch.setattr(service, "configured_dispatcher", Mock(side_effect=AssertionError("PR dispatcher used")))
    await service.reconcile_repository_ingestion(store, http)
    service.configured_ingestion_dispatcher.assert_called_once_with(http)
    reconcile.assert_awaited_once_with(jobs, dispatcher)
