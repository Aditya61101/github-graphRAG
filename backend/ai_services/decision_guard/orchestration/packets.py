"""Provider transport checkpoints, without changing assessment/proof semantics."""

import json

from ai_services.decision_guard.context import conservative_tokens
from ai_services.decision_guard.evaluation.model_adapter import wire_text
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    ChallengeBatchOutput,
    DispatchDenied,
    ModelReply,
)
from ai_services.decision_guard.models import digest


class DurableEvaluationModel:
    def __init__(
        self, model, store, job_id, token, settings, counter=conservative_tokens
    ):
        self.model, self.store, self.job_id, self.token = model, store, job_id, token
        self.settings, self.counter = settings, counter
        self.deployment_revision = model.deployment_revision

    def key(self, request, phase):
        # Includes original identities, expanded evidence, authority, prompts,
        # schema and deployment revision. No reuse across a changed request.
        return digest(
            json.dumps(
                [
                    request.model_dump(mode="json"),
                    wire_text(request, phase),
                    self.deployment_revision,
                    self.settings.max_output_tokens,
                ],
                sort_keys=True,
            )
        )

    async def cached_reply(self, request, phase):
        raw = self.store.cached_packet(
            self.job_id, self.token, self.key(request, phase)
        )
        return ModelReply.model_validate_json(raw) if raw else None

    @staticmethod
    def validate_reply(request, phase, reply):
        reply = ModelReply.model_validate_json(reply.model_dump_json())
        if reply.status != "OK":
            return reply, "FAILED"
        schema = AssessmentBatchOutput if phase == "ASSESS" else ChallengeBatchOutput
        output = schema.model_validate_json(reply.output_json or "")
        expected = (
            {u.unit_id for u in request.units}
            if phase == "ASSESS"
            else {c.proposal_id for c in request.candidates}
        )
        ids = (
            [u.unit_id for u in output.units]
            if phase == "ASSESS"
            else [d.proposal_id for d in output.decisions]
            + [r.subject_id for r in output.rejections]
        )
        if (
            output.batch_id != request.batch_id
            or set(ids) != expected
            or len(ids) != len(expected)
        ):
            raise ValueError("Provider response manifest mismatch")
        return reply, "COMPLETED"

    async def _call(self, request, phase, max_output_tokens, timeout):
        key = self.key(request, phase)
        cached = await self.cached_reply(request, phase)
        if cached:
            return cached
        if not getattr(self.model, "dispatch_allowed", True):
            raise DispatchDenied("AUTHORIZATION_DENIED")
        inp = self.counter(wire_text(request, phase)) + self.settings.provider_reserve
        reserve = inp + max_output_tokens
        cost = (
            inp * (self.settings.input_rate_per_million or 0)
            + max_output_tokens * (self.settings.output_rate_per_million or 0)
        ) / 1_000_000
        denied = self.store.reserve_packet(
            self.job_id,
            self.token,
            key,
            phase,
            request.model_dump_json(),
            reserve,
            cost,
        )
        if denied:
            raise DispatchDenied("BUDGET_DENIED")
        reply = None
        try:
            method = self.model.assess if phase == "ASSESS" else self.model.challenge
            reply = await method(
                request, max_output_tokens=max_output_tokens, timeout=timeout
            )
            reply, state = self.validate_reply(request, phase, reply)
        except DispatchDenied:
            self.store.void_packet(self.job_id, self.token, key)
            raise
        except ValueError:
            # A received invalid response can be retried within persisted limits.
            self.store.save_packet(self.job_id, self.token, key, reply, "FAILED")
            raise
        except BaseException:
            # Timeout/cancellation/network failure may follow provider execution.
            # Leave IN_FLIGHT and full reservation; automatic retries must not
            # pretend the request was never executed.
            raise
        self.store.save_packet(self.job_id, self.token, key, reply, state)
        return reply

    async def assess(self, request, *, max_output_tokens, timeout):
        return await self._call(request, "ASSESS", max_output_tokens, timeout)

    async def challenge(self, request, *, max_output_tokens, timeout):
        return await self._call(request, "CHALLENGE", max_output_tokens, timeout)
