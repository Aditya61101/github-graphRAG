"""Gemini wire transport with the original role/quote/span domain validation."""
from pydantic import ValidationError

from shared.ai.vertex import response_status
from .model_adapter import ASSESS_PROMPT, CHALLENGE_PROMPT
from .models import ModelReply, ResponseDiagnostic, DispatchDenied
from .wire import WireAssessment, WireChallenge, provider_request_json


class VertexEvaluationModel:
    def __init__(self, llm, *, deployment_revision):
        self.llm, self.deployment_revision = llm, deployment_revision
        self.provider, self.provider_identity = "google", llm.identity

    @property
    def dispatch_allowed(self):
        return bool(self.deployment_revision and self.deployment_revision != "UNCONFIGURED")

    async def _call(self, request, phase, max_output_tokens, timeout):
        if not self.dispatch_allowed:
            raise DispatchDenied("AUTHORIZATION_DENIED")
        schema = WireAssessment if phase == "ASSESS" else WireChallenge
        response = await self.llm.generate(provider_request_json(request, phase), schema=schema,
            system=ASSESS_PROMPT if phase == "ASSESS" else CHALLENGE_PROMPT,
            max_output_tokens=max_output_tokens, timeout=timeout)
        status = response_status(response)
        parsed, output_json, diagnostics = None, None, []
        if status == "OK":
            try:
                parsed = schema.model_validate_json(response.text or "")
                if phase == "ASSESS":
                    expanded, unused = parsed.expanded_with_diagnostics(request)
                    if unused:
                        diagnostics.append(ResponseDiagnostic(stage="QUOTE_EXPANSION",
                            code="UNUSED_QUOTES_IGNORED", quote_ids=unused))
                else:
                    expanded = parsed.expanded(request)
                output_json = expanded.model_dump_json()
            except (ValueError, ValidationError) as error:
                status = "INVALID"
                known = {"DUPLICATE_QUOTE_DEFINITION", "UNKNOWN_QUOTE_ID", "UNKNOWN_SOURCE_REF",
                         "UNUSED_QUOTE_DEFINITION", "QUOTE_SOURCE_ROLE_MISMATCH", "WRONG_REVISION_CITATION"}
                diagnostics.append(ResponseDiagnostic(
                    stage="QUOTE_EXPANSION" if phase == "ASSESS" else "SPAN_RESOLUTION",
                    code=str(error) if str(error) in known else "INVALID_EXPANDED_SCHEMA"))
        usage = response.usage_metadata
        thoughts = getattr(usage, "thoughts_token_count", None) if usage else None
        visible = getattr(usage, "candidates_token_count", None) if usage else None
        # Unknown usage remains unknown: the engine retains the full reservation.
        output = (visible + thoughts if visible is not None and thoughts is not None else None)
        return ModelReply(output_json=output_json, status=status,
            input_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            output_tokens=output, thinking_tokens=thoughts,
            cached_input_tokens=getattr(usage, "cached_content_token_count", None) if usage else None,
            request_id=getattr(response, "response_id", None),
            provider="google", model_version=getattr(response, "model_version", None),
            provider_identity=self.provider_identity,
            wire_output_json=parsed.model_dump_json() if parsed else None,
            response_diagnostics=tuple(diagnostics))

    async def assess(self, request, *, max_output_tokens, timeout):
        return await self._call(request, "ASSESS", max_output_tokens, timeout)

    async def challenge(self, request, *, max_output_tokens, timeout):
        return await self._call(request, "CHALLENGE", max_output_tokens, timeout)
