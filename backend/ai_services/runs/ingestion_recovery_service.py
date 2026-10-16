"""Private Cloud Run endpoint; require authenticated IAM invocation, like Session B."""
import httpx
from fastapi import FastAPI
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs
from ai_services.ingestion.dispatch import configured_dispatcher, reconcile
from shared.utils.processing import require_background_processing

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/reconcile")
async def recover():
    require_background_processing()
    store = SqliteApplicationStore()
    if store.session_factory.kw["bind"].dialect.name != "postgresql":
        raise RuntimeError("Remote recovery requires PostgreSQL")
    async with httpx.AsyncClient(timeout=30) as http:
        await reconcile(IngestionJobs(store.session_factory), configured_dispatcher(http))
    return {"status": "reconciled"}
