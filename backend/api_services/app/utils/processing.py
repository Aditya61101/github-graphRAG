"""HTTP processing boundary for the initial API-only deployment."""
from fastapi import HTTPException

from api_services.app import config


def require_processing_enabled():
    if not config.ENABLE_BACKGROUND_PROCESSING:
        raise HTTPException(
            status_code=503,
            detail="Repository ingestion, synchronization, ADR processing and PR evaluation are disabled on this service. No processing was queued.",
        )
