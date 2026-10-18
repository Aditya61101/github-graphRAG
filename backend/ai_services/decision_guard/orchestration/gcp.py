"""Cloud Tasks holds a dispatch slot until the private HTTP worker completes."""

import asyncio
import base64
import hashlib
import json
import os
import time
from urllib.parse import urlparse

from sqlalchemy.engine import make_url


def validate_remote_configuration():
    if make_url(os.environ.get("DATABASE_URL", "sqlite://")).get_backend_name() != "postgresql":
        raise ValueError("Remote evaluation requires shared PostgreSQL")
    if not os.getenv("ADR_GCS_BUCKET"):
        raise ValueError("Remote evaluation requires ADR_GCS_BUCKET")


async def access_token():
    def resolve():
        import google.auth
        from google.auth.transport.requests import Request

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(Request())
        return credentials.token

    return await asyncio.to_thread(resolve)


class CloudTasksWorkerDispatcher:
    def __init__(self, client, token, queue, worker_url, service_account, *, clock=time.time):
        parsed = urlparse(worker_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Worker URL must be an HTTPS service origin")
        if not queue.startswith("projects/") or "/locations/" not in queue or "/queues/" not in queue:
            raise ValueError("Expected Cloud Tasks queue resource")
        self.client, self.token, self.queue = client, token, queue
        self.url, self.service_account, self.clock = worker_url.rstrip("/"), service_account, clock

    async def dispatch(self, job_id):
        # A time bucket deduplicates concurrent recovery passes, while permitting
        # recovery after a successfully delivered task or explicit continuation.
        identity = hashlib.sha256(f"{job_id}:{int(self.clock() // 300)}".encode()).hexdigest()
        response = await self.client.post(
            f"https://cloudtasks.googleapis.com/v2/{self.queue}/tasks",
            headers={"Authorization": "Bearer " + await self.token()},
            json={"task": {
                "name": f"{self.queue}/tasks/{identity}",
                "dispatchDeadline": "1800s",
                "httpRequest": {
                    "httpMethod": "POST", "url": self.url + "/evaluate",
                    "headers": {"Content-Type": "application/json"},
                    "body": base64.b64encode(json.dumps({"job_id": job_id}).encode()).decode(),
                    "oidcToken": {"serviceAccountEmail": self.service_account, "audience": self.url},
                },
            }},
        )
        if response.status_code != 409:
            response.raise_for_status()


def configured_dispatcher(client):
    mode = os.getenv("PR_EVALUATION_DISPATCHER", "local")
    if mode == "local":
        if os.getenv("K_SERVICE") or os.getenv("APP_ENV", "").lower() == "production":
            from shared.utils.processing import background_processing_enabled
            if background_processing_enabled():
                raise ValueError("Production processing requires cloud_tasks dispatch")
        return None
    if mode != "cloud_tasks":
        raise ValueError("PR_EVALUATION_DISPATCHER must be local or cloud_tasks")
    validate_remote_configuration()
    return CloudTasksWorkerDispatcher(client, access_token, os.environ["PR_EVALUATION_QUEUE"],
        os.environ["PR_EVALUATION_WORKER_URL"], os.environ["PR_EVALUATION_TASK_SERVICE_ACCOUNT"])
