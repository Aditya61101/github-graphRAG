import pytest
from types import SimpleNamespace

from ai_services.runs import run_stage_4_1_semantic_retrieval_validation as live


@pytest.mark.asyncio
async def test_capture_persists_usage_and_blocks_second_request(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'out', lambda n: tmp_path / (n + '.json'))
    calls = []
    class Response:
        model = 'text-embedding-3-large'
        usage = SimpleNamespace(model_dump=lambda **kw: {'prompt_tokens': 687, 'total_tokens': 687})
        def model_dump(self, **kw):
            return {'data': [], 'usage': self.usage.model_dump()}
    async def create(**kw):
        calls.append(kw)
        return Response()
    client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    capture = live.CaptureClient(client, 'preserved')
    await capture.create(model='deployment', input=['preserved'])
    with pytest.raises(AssertionError):
        await capture.create(model='deployment', input=['preserved'])
    assert len(calls) == 1
    assert live.read(tmp_path / 'embedding_usage.json')['provider_usage']['total_tokens'] == 687


@pytest.mark.asyncio
async def test_persistent_marker_blocks_process_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'out', lambda n: tmp_path / (n + '.json'))
    live.save(tmp_path / 'attempt.json', {'prior': True})
    async def forbidden(**kw):
        raise AssertionError('Provider must never be reached')
    capture = live.CaptureClient(SimpleNamespace(embeddings=SimpleNamespace(create=forbidden)), 'preserved')
    with pytest.raises(FileExistsError):
        await capture.create(model='deployment', input=['preserved'])


@pytest.mark.asyncio
async def test_uncertain_failure_is_preserved_without_retry_or_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'out', lambda n: tmp_path / (n + '.json'))
    calls = []
    async def fail(**kw):
        calls.append(kw)
        raise TimeoutError('secret endpoint must not be recorded')
    capture = live.CaptureClient(SimpleNamespace(embeddings=SimpleNamespace(create=fail)), 'preserved')
    with pytest.raises(TimeoutError):
        await capture.create(model='deployment', input=['preserved'])
    assert len(calls) == 1
    failure = live.read(tmp_path / 'embedding_failure.json')
    assert failure['error_type'] == 'TimeoutError'
    assert 'secret' not in str(failure)
