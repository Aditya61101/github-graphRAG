from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.pr.models import PullRequestEvent
from api_services.app.config import GITHUB_APP_WEBHOOK_SECRET
from api_services.app.utils.github_app_access import get_github_app
from api_services.app import config
from api_services.app.utils.processing import require_processing_enabled

logger = logging.getLogger(__name__)
router = APIRouter()


def verify_github_signature(raw_body, signature_header, secret):
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header)


async def _process_push_in_background(service, payload):
    require_processing_enabled()
    try:
        await service.handle_github_push_webhook(payload)
    except Exception:
        logger.exception("Webhook indexing failed")


PR_INGESTION_ACTIONS = {"opened", "synchronize", "reopened"}
INSTALLATION_ACTIONS = {
    "installation": {
        "created",
        "deleted",
        "suspend",
        "unsuspend",
        "new_permissions_accepted",
    },
    "installation_repositories": {"added", "removed"},
}


async def _verified_payload(request, signature):
    configured = getattr(request.app.state, "github_app", None)
    secret = (
        configured.settings.webhook_secret if configured else GITHUB_APP_WEBHOOK_SECRET
    )
    if not secret:
        raise HTTPException(500, "Webhook secret is not configured")
    raw = await request.body()
    if not verify_github_signature(raw, signature, secret):
        raise HTTPException(401, "Invalid GitHub webhook signature")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError()
        return payload
    except (TypeError, ValueError, UnicodeError):
        raise HTTPException(400, "Malformed webhook JSON object") from None


def _parse_pr_event(payload, delivery_id):
    try:
        return PullRequestEvent.from_webhook(payload, delivery_id)
    except (ValueError, ValidationError):
        raise HTTPException(
            400, "Invalid or incomplete pull request webhook metadata"
        ) from None


def _installation_id(payload):
    installation = payload.get("installation")
    if not isinstance(installation, dict) or not isinstance(
        installation.get("id"), int
    ):
        raise HTTPException(400, "Webhook installation ID is required")
    return str(installation["id"])


def _tracked_repository(app_service, payload, installation_id):
    repo = payload.get("repository")
    if not isinstance(repo, dict) or not isinstance(repo.get("id"), int):
        raise HTTPException(400, "Webhook repository ID is required")
    tracked = app_service.store.get_repository(str(repo["id"]))
    if (
        not tracked
        or tracked.installation_id != installation_id
        or repo.get("private", True)
    ):
        return None
    return tracked


async def _handle_authorization(app_service, payload, event):
    from sqlalchemy import select

    from ai_services.ingestion.persistence.models import GitHubConnectionModel

    sender = payload.get("sender")
    if (
        payload.get("action") != "revoked"
        or not isinstance(sender, dict)
        or not isinstance(sender.get("id"), int)
    ):
        raise HTTPException(400, "Invalid authorization event")
    # A still-valid current token may reflect reauthorization after a late event.
    with app_service.store.session_factory() as session:
        connection = session.scalars(
            select(GitHubConnectionModel).where(
                GitHubConnectionModel.github_user_id == str(sender["id"])
            )
        ).first()
    if connection:
        try:
            token = await app_service.user_token(connection.user_id)
            connection = app_service.store.get_user_github_connection(
                connection.user_id
            )
            await app_service.request("GET", "/user", token)
        except GitHubAppError as exc:
            if exc.reconnect_required:
                # The token resolver already invalidated the rejected credential.
                pass
            elif exc.status_code == 401:
                # A reauthorization/rotation may complete during the probe.
                app_service.revoke_user(str(sender["id"]), connection.access_token)
            else:
                raise
    return {"status": "reconciled", "event": event}


async def _handle_installation(app_service, payload, event, installation_id):
    if payload.get("action") not in INSTALLATION_ACTIONS[event]:
        return {"status": "ignored", "event": event}
    # Reconcile current inventory, never apply potentially stale payload deltas.
    app_service.invalidate(installation_id)
    await app_service.reconcile(installation_id)
    return {"status": "reconciled", "event": event}


async def _queue_pr(request, background_tasks, normalized_pr, tracked):
    require_processing_enabled()
    if (
        normalized_pr.base_repository_id != normalized_pr.github_repository_id
        or normalized_pr.repository_full_name.lower() != tracked.full_name.lower()
    ):
        raise HTTPException(400, "Pull request repository identity mismatch")
    orchestrator = getattr(request.app.state, "evaluation_orchestrator", None)
    if orchestrator is None:
        raise HTTPException(503, "PR evaluation orchestration is not initialized")
    from sqlalchemy.exc import SQLAlchemyError
    from ai_services.decision_guard.orchestration.profiles import (
        EvaluationConfigurationError,
    )

    try:
        job_id = await orchestrator.accept(tracked.id, normalized_pr, background_tasks)
    except EvaluationConfigurationError as error:
        raise HTTPException(503, str(error)) from None
    except SQLAlchemyError:
        # Never acknowledge a job whose durable transaction did not commit.
        raise HTTPException(
            503, "PR evaluation could not be persisted; retry delivery"
        ) from None
    except ValueError:
        raise HTTPException(400, "Webhook delivery identity mismatch") from None
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "event": "pull_request",
            "repository_id": tracked.id,
            "pull_request_number": normalized_pr.pull_request_number,
            "revision_key": normalized_pr.revision_key,
            "evaluation_job_id": job_id,
        },
    )


async def _queue_push(request, background_tasks, payload, tracked):
    ref = payload.get("ref")
    if (
        not isinstance(ref, str)
        or ref != "refs/heads/" + tracked.tracked_branch
        or not payload.get("after")
        or payload["after"] == "0" * 40
    ):
        return {"status": "ignored", "event": "push"}
    require_processing_enabled()
    from api_services.app.utils.ingestion import accept
    run_id = await accept(request, background_tasks, tracked, target=payload["after"],
        trigger="webhook_push", delivery_id=request.headers.get("X-GitHub-Delivery"))
    return JSONResponse(
        status_code=202, content={"status": "accepted", "event": "push", "run_id": run_id}
    )


@router.post("")
@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    x_github_delivery: str | None = Header(None, alias="X-GitHub-Delivery"),
):
    payload = await _verified_payload(request, x_hub_signature_256)
    event = x_github_event or "unknown"
    if event == "ping":
        return {"status": "ok", "event": "ping"}

    normalized_pr = None
    if event == "pull_request":
        if payload.get("action") in PR_INGESTION_ACTIONS:
            normalized_pr = _parse_pr_event(payload, x_github_delivery)
        elif payload.get("action") != "closed":
            return {"status": "ignored", "event": event}

    app_service = get_github_app(request)
    try:
        if event == "github_app_authorization":
            return await _handle_authorization(app_service, payload, event)
        installation_id = _installation_id(payload)
        if event in INSTALLATION_ACTIONS:
            return await _handle_installation(
                app_service, payload, event, installation_id
            )
        if event not in {"push", "pull_request"}:
            return {"status": "ignored", "event": event}

        tracked = _tracked_repository(app_service, payload, installation_id)
        if tracked is None:
            return {"status": "ignored", "event": event}
        if normalized_pr is not None:
            require_processing_enabled()
            # Metadata is synchronized from GitHub, never from arrival order.
            from ai_services.decision_guard.orchestration.projections import (
                request_sync,
                synchronize,
            )

            orchestrator = getattr(request.app.state, "evaluation_orchestrator", None)
            if orchestrator is not None:
                request_sync(
                    orchestrator.store, tracked.id, normalized_pr.pull_request_number
                )
                try:
                    await synchronize(
                        orchestrator.store,
                        app_service,
                        tracked.id,
                        normalized_pr.pull_request_number,
                    )
                except GitHubAppError:
                    pass  # both metadata and evaluation now have durable recovery
            return await _queue_pr(request, background_tasks, normalized_pr, tracked)
        if event == "push":
            return await _queue_push(request, background_tasks, payload, tracked)
        if not config.ENABLE_BACKGROUND_PROCESSING:
            pr = payload.get("pull_request") or {}
            return {
                "status": "acknowledged" if pr.get("merged") is True else "ignored",
                "event": event,
                "message": "Processing is disabled; no synchronization was queued.",
            }
        from ai_services.decision_guard.orchestration.projections import (
            request_sync,
            synchronize,
        )

        orchestrator = getattr(request.app.state, "evaluation_orchestrator", None)
        if orchestrator is None:
            raise HTTPException(503, "PR evaluation orchestration is not initialized")
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if type(number) is not int or number <= 0:
            raise HTTPException(400, "Pull request number is required")
        request_sync(orchestrator.store, tracked.id, number)
        try:
            await synchronize(orchestrator.store, app_service, tracked.id, number)
        except GitHubAppError:
            return JSONResponse(
                status_code=202,
                content={"status": "metadata_sync_queued", "event": event},
            )
        return {"status": "synchronized", "event": event}
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
