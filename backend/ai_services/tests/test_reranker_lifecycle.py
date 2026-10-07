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
async def test_fastapi_lifespan_shares_one_model_with_dependencies_and_agent(monkeypatch):
    import api_services.app.main as main

    reranker = SimpleNamespace()
    factory = MagicMock(return_value=reranker)
    monkeypatch.setattr(main, "Qwen3Reranker", factory)
    monkeypatch.setattr(main, "require_env", lambda name, default=None: default or "test")
    driver = MagicMock()
    monkeypatch.setattr(main, "GraphDatabase", SimpleNamespace(driver=MagicMock(return_value=driver)))
    for name in (
        "AsyncAzureOpenAI", "AzureOpenAIEmbedder", "AzureChatOpenAI", "AzureOpenAILLM",
        "SqliteApplicationStore", "SqliteCredentialProvider", "GitHubRepositorySource",
        "RepositoryIngestionService", "ADRNeo4jWriter", "ADRProcessingService",
        "ADRChunker", "ADRArchitecturalExtractor", "ADREntityResolver", "ADRService",
        "Neo4jGraphRepository", "RepositoryGraphService",
    ):
        monkeypatch.setattr(main, name, MagicMock())
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(main, "create_retrievers", MagicMock(return_value=(object(), object(), object())))
    agent_factory = MagicMock()
    monkeypatch.setattr(main, "RAGQueryAgent", agent_factory)
    app = FastAPI()
    async with main.lifespan(app):
        assert app.state.reranker is reranker
        assert app.state.deps.reranker is reranker
        assert agent_factory.call_args.kwargs["reranker"] is reranker
        assert agent_factory.call_args.kwargs["retrieval_settings"] is main.RETRIEVAL_SETTINGS
        factory.assert_called_once_with(main.RETRIEVAL_SETTINGS)
    driver.close.assert_called_once()


@pytest.mark.asyncio
async def test_model_load_failure_fails_startup(monkeypatch):
    import api_services.app.main as main

    factory = MagicMock(side_effect=RuntimeError("model unavailable"))
    monkeypatch.setattr(main, "Qwen3Reranker", factory)
    monkeypatch.setattr(main, "require_env", lambda name: "neo4j")
    with pytest.raises(RuntimeError, match="model unavailable"):
        async with main.lifespan(FastAPI()):
            pytest.fail("Startup should not yield after a model-load failure")
    factory.assert_called_once()
