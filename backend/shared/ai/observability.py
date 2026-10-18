"""Allowlisted Vertex request metadata; never serialize request/exception bodies."""
import asyncio
import json
import logging
import re

logger = logging.getLogger("decisionguard.ai.requests")
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)
logger.propagate = False


def error_classification(error):
    code = getattr(error, "code", None)
    if code == 504:
        return "provider_deadline"
    if code == 429:
        return "provider_rate_limit"
    if isinstance(code, int) and 500 <= code < 600:
        return "provider_server"
    if isinstance(error, TimeoutError):
        return "local_deadline"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if type(error).__name__ == "ValidationError":
        return "schema_validation"
    if type(error).__name__ == "VertexResponseError":
        return "response_validation"
    return "request_failure"


def request_id(value):
    headers = getattr(getattr(value, "sdk_http_response", None), "headers", None)
    if not headers:
        headers = getattr(getattr(value, "response", None), "headers", None) or {}
    identifier = getattr(value, "response_id", None) or headers.get("x-request-id") or headers.get("x-goog-request-id")
    # No arbitrary headers, control characters, or provider error messages.
    if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", identifier):
        return identifier
    return None


def record_batch(*, model, observation, prompt, system, schema, elapsed, response=None, error=None):
    record = dict(event="ai_batch", operation="repository_extraction", model=model,
        chunk_count=observation["chunk_count"], raw_bytes=observation["raw_bytes"],
        user_bytes=len(prompt.encode("utf-8")), system_bytes=len((system or "").encode("utf-8")),
        schema_bytes=len(json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
        elapsed_seconds=round(elapsed, 3), outcome="error" if error is not None else "success",
        error_classification=error_classification(error) if error is not None else None,
        provider_request_id=request_id(error if error is not None else response))
    record["rendered_bytes"] = record["user_bytes"] + record["system_bytes"] + record["schema_bytes"]
    # Rendered bytes measure textual content/schema, not SDK HTTP framing.
    logger.info(json.dumps(record, separators=(",", ":")))
