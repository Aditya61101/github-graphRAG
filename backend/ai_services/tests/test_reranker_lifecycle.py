from contextlib import nullcontext
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from ai_services.retrievers.reranker import Qwen3Reranker
from ai_services.retrievers.settings import RetrievalSettings
from ai_services.retrievers.evidence import EvidenceCandidate


def test_qwen_uses_causal_yes_no_scoring_left_padding_and_loads_once(monkeypatch):
    tokenizer = MagicMock()
    tokenizer.convert_tokens_to_ids.side_effect = lambda token: {"no": 10, "yes": 20}[token]
    tokenizer.encode.side_effect = [[11], [22]]
    tokenizer.side_effect = lambda *args, **kwargs: {"input_ids": [[3, 4]]}
    tensor = MagicMock()
    tokenizer.pad.return_value = {"input_ids": tensor}
    model = MagicMock()
    loader = MagicMock()
    loader.from_pretrained.return_value.to.return_value.eval.return_value = model
    token_loader = MagicMock()
    token_loader.from_pretrained.return_value = tokenizer
    torch = MagicMock()
    torch.cuda.is_available.return_value = False
    torch.inference_mode.side_effect = nullcontext
    torch.softmax.return_value.__getitem__.return_value.cpu.return_value.tolist.return_value = [0.9]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=token_loader, AutoModelForCausalLM=loader,
    ))
    settings = RetrievalSettings(reranker_batch_size=1)
    reranker = Qwen3Reranker(settings)
    chunk = EvidenceCandidate("B", "middleware code", "setup.py", "repo")
    first = reranker.rerank("middlewares?", [chunk])
    second = reranker.rerank("middlewares?", [chunk])
    assert first == second
    assert first[0].rerank_score == 0.9
    loader.from_pretrained.assert_called_once_with(settings.reranker_model)
    token_loader.from_pretrained.assert_called_once_with(settings.reranker_model, padding_side="left")
    torch.device.assert_called_once_with("cpu")
    assert tokenizer.pad.call_args.args[0]["input_ids"] == [[11, 3, 4, 22]]
    assert "File: setup.py" in tokenizer.call_args.args[0][0]
    assert model.call_args.kwargs["logits_to_keep"] == 1
    assert model.call_args.kwargs["use_cache"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('processing', [True, False])
async def test_fastapi_lifespan_needs_no_local_model(monkeypatch, processing):
    import api_services.app.main as main
    monkeypatch.setattr(main.config, 'ENABLE_BACKGROUND_PROCESSING', processing)
    from ai_services.decision_guard.orchestration.dispatch import EvaluationOrchestrator
    from unittest.mock import AsyncMock
    recovery = AsyncMock()
    monkeypatch.setattr(EvaluationOrchestrator, 'recover_forever', recovery)

    factory = MagicMock(side_effect=AssertionError("No model loading at startup"))
    monkeypatch.setattr(Qwen3Reranker, "__init__", factory)
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "transformers", None)
    monkeypatch.setattr(main, "require_env", lambda name, default=None: default or "test")
    monkeypatch.setattr(main.GitHubAppSettings, 'from_env', MagicMock(return_value=MagicMock()))
    driver = MagicMock()
    monkeypatch.setattr(main, "GraphDatabase", SimpleNamespace(driver=MagicMock(return_value=driver)))
    for name in (
        "SqliteApplicationStore", "GitHubAppService", "GitHubOAuth", "GitHubRepositorySource",
        "RepositoryIngestionService", "PullRequestIngestionService", "ADRNeo4jWriter", "ADRProcessingService",
        "ADRChunker", "ADRArchitecturalExtractor", "ADREntityResolver", "ADRService",
        "Neo4jGraphRepository", "RepositoryGraphService",
    ):
        monkeypatch.setattr(main, name, MagicMock())
    provider = SimpleNamespace(client=object(), embedder=SimpleNamespace(dimensions=3072),
        llm=SimpleNamespace(client=object(), deployment="offline"),
        chat_model=MagicMock(return_value=object()), aclose=AsyncMock())
    monkeypatch.setattr(main, "create_provider", MagicMock(return_value=provider))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(main, "create_retrievers", MagicMock(return_value=(object(), object(), object())))
    agent_factory = MagicMock()
    monkeypatch.setattr(main, "RAGQueryAgent", agent_factory)
    app = FastAPI()
    async with main.lifespan(app):
        assert app.state.reranker is None
        assert app.state.deps.reranker is None
        assert agent_factory.call_args.kwargs["reranker"] is None
        assert agent_factory.call_args.kwargs["retrieval_settings"] is main.RETRIEVAL_SETTINGS
        factory.assert_not_called()
        assert app.state.pr_ingestion_service is app.state.deps.pr_ingestion_service
        assert main.PullRequestIngestionService.call_args.kwargs['repository_source'] is main.GitHubRepositorySource.return_value
        assert main.PullRequestIngestionService.call_args.kwargs['repository_lock'] == main.RepositoryIngestionService.return_value._get_lock
        if not processing:
            recovery.assert_not_called()
            main.SqliteApplicationStore.return_value.recover_interrupted_pr_revisions.assert_not_called()
            main.ADRNeo4jWriter.return_value.initialize_constraints.assert_not_called()
    driver.close.assert_called_once()
