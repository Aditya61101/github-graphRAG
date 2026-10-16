"""One shared SQL epoch + Neo4j seal check around materialized graph reads."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
from fastapi import HTTPException
from .persistence.models import RepositoryModel, RepositoryIngestionState


class GraphUnavailable(HTTPException):
    def __init__(self):
        super().__init__(503, "Repository graph update or repair is in progress; retry later")

read_epoch = ContextVar("graph_read_epoch", default=None)


def guarded_read(function):
    """All graph services share this barrier, rather than endpoint-specific rules."""
    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        barrier = getattr(self, "barrier", None)
        if barrier is None:
            return await function(self, *args, **kwargs)
        bound = inspect.signature(function).bind(self, *args, **kwargs).arguments
        repo_id = bound.get("repository_id", bound.get("repository_identifier"))
        async with barrier.read(repo_id):
            return await function(self, *args, **kwargs)
    return wrapped


def guarded_route(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        bound = inspect.signature(function).bind(*args, **kwargs).arguments
        request = bound["request"]
        barrier = getattr(request.app.state, "graph_barrier", None)
        if barrier is None:
            return await function(*args, **kwargs)
        from api_services.app.utils.github_app_access import authorize_repository_read
        repo = await authorize_repository_read(request, bound["current_user"].id, bound["repo_id"])
        async with barrier.read(repo.id):
            return await function(*args, **kwargs)
    wrapped.__signature__ = inspect.signature(function, eval_str=True)
    return wrapped

class GraphReadBarrier:
    def __init__(self, sessions, control):
        self.sessions, self.control = sessions, control

    def snapshot(self, repo_id):
        with self.sessions() as s:
            repo, state = s.get(RepositoryModel, repo_id), s.get(RepositoryIngestionState, repo_id)
            if (not repo or not state or state.state != "READY" or repo.status != "COMPLETED"
                    or not repo.indexed_commit_sha or not state.published_run_id):
                raise GraphUnavailable()
            expected = (state.epoch, state.published_run_id, state.published_token, repo.indexed_commit_sha)
        try:
            seal = self.control.snapshot(repo_id)
        except Exception:
            raise GraphUnavailable() from None
        if not seal or (seal["state"], seal["run_id"], seal["token"], seal["target"]) != (
                "COMPLETE", expected[1], expected[2], expected[3]):
            raise GraphUnavailable()
        return expected

    @asynccontextmanager
    async def read(self, repo_id):
        import asyncio
        before = await asyncio.to_thread(self.snapshot, repo_id)
        marker = read_epoch.set((repo_id, before))
        try:
            yield before
            if await asyncio.to_thread(self.snapshot, repo_id) != before:
                raise GraphUnavailable()
        finally:
            read_epoch.reset(marker)
