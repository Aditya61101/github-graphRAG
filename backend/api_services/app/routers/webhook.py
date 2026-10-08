from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ai_services.ingestion.service import RepositoryIngestionService
from api_services.app.config import GITHUB_APP_WEBHOOK_SECRET

logger = logging.getLogger(__name__)

router = APIRouter()


def get_ingestion_service(request: Request) -> RepositoryIngestionService:
    service = getattr(request.app.state, "ingestion_service", None)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Repository ingestion service is not initialized.",
        )
    return service


def verify_github_signature(
    raw_body: bytes,
    signature_header: str | None,
    secret: str | None,
) -> bool:
    """Validate webhook HMAC signature strictly. Fails closed if secret or signature is missing."""
    if not secret or not signature_header:
        return False

    hash_prefix = "sha256="
    if not signature_header.startswith(hash_prefix):
        return False

    expected_signature = hash_prefix + hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_header)


async def _process_push_in_background(
    service: RepositoryIngestionService,
    payload: dict[str, Any],
) -> None:
    try:
        result = await service.handle_github_push_webhook(payload)
        logger.info(
            f"Webhook push sync finished for '{result.repository}': status={result.status}, msg={result.message}"
        )
    except Exception as exc:
        logger.exception(f"Unexpected error during webhook background push sync: {exc}")


@router.post("")
@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> Any:
    """Handle GitHub webhooks (push, pull_request, ping) with fail-closed HMAC verification."""
    if not GITHUB_APP_WEBHOOK_SECRET:
        logger.error("GITHUB_APP_WEBHOOK_SECRET is not configured; refusing webhook request.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook secret is not configured on the server.",
        )

    raw_body = await request.body()

    if not verify_github_signature(raw_body, x_hub_signature_256, GITHUB_APP_WEBHOOK_SECRET):
        logger.warning("Rejected webhook request: invalid or missing X-Hub-Signature-256")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature.",
        )

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed JSON payload: {exc}",
        )

    service = get_ingestion_service(request)
    event_type = x_github_event or "unknown"

    if event_type == "ping":
        return {
            "status": "ok",
            "event": "ping",
            "zen": payload.get("zen"),
            "hook_id": payload.get("hook_id"),
        }

    if event_type == "push":
        # Check for branch deletion
        after_sha = payload.get("after")
        if after_sha == "0000000000000000000000000000000000000000":
            return {"status": "ignored", "event": "push", "message": "Branch deletion ignored."}

        # Queue incremental ingestion in background
        background_tasks.add_task(_process_push_in_background, service, payload)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "accepted",
                "event": "push",
                "repository": payload.get("repository", {}).get("full_name"),
                "ref": payload.get("ref"),
                "message": "Push received. Incremental sync scheduled in background.",
            },
        )

    if event_type == "pull_request":
        # PR merges are recorded; canonical indexing runs upon the corresponding branch push.
        result = await service.handle_github_pull_request_webhook(payload)
        return {
            "status": result.status,
            "event": "pull_request",
            "repository": result.repository,
            "message": result.message,
            "indexed_commit_sha": result.indexed_commit_sha,
        }

    return {
        "status": "ignored",
        "event": event_type,
        "message": f"Webhook event '{event_type}' is not monitored for RKG synchronization.",
    }
