"""Frozen synthetic ClaimIQ guard contract through mocked Gemini wire responses.

This is not a replay of private ClaimIQ original uploads or a live accuracy test.
Exact archived-source replay remains a release gate requiring original artifacts.
"""
import json
from types import SimpleNamespace

import pytest

from ai_services.decision_guard.evaluation.models import AssessmentBatchOutput, ChallengeBatchOutput
from ai_services.decision_guard.evaluation.vertex_adapter import VertexEvaluationModel
from ai_services.decision_guard.evaluation.wire import compact
from ai_services.tests.test_decision_guard_context import changed
from ai_services.tests.test_decision_guard_evaluation import ScriptedModel, evaluate, fixture, refs
from ai_services.tests.test_proof_scoping import edit_output
from ai_services.tests.test_google_migration import config, llm, response

RULE = "Accepted decision: readiness requires every active document to be VERIFIED. Active documents may also be PENDING. Inactive documents do not govern readiness."
BEFORE = "def readiness(documents):\n    statuses = {d.status for d in documents if d.active}\n    if statuses == {'VERIFIED'}:\n        return True\n    return False\n"
AFTER = BEFORE.replace("statuses == {'VERIFIED'}", "'VERIFIED' in statuses")
SAFE = AFTER.replace("    if 'VERIFIED'", "    if any(d.active and d.status != 'VERIFIED' for d in documents):\n        return False\n    if 'VERIFIED'")


def run(text, active):
    namespace = {}
    exec(compile(text, "frozen-claimiq-synthetic-control", "exec"), namespace)
    return namespace['readiness']([SimpleNamespace(status='VERIFIED', active=True),
        SimpleNamespace(status='PENDING', active=active)])


class ClaimIQOracle(ScriptedModel):
    def __init__(self, head, active, attribution):
        super().__init__({('ADR', 'service.py'): 'conflict'}, attribution=attribution)
        self.head, self.active = head, active

    async def assess(self, request, **kwargs):
        reply = await super().assess(request, **kwargs)
        def transform(proposal):
            unit = next(u for u in request.units if u.unit_id == proposal.unit_id)
            return proposal.model_copy(update={'issues': tuple(issue.model_copy(update={
                'claim': 'Readiness can accept an active VERIFIED document alongside an active PENDING document',
                'consequence': 'The active document set incorrectly satisfies the readiness contract',
                'core': issue.core.model_copy(update={
                    'boundary': 'readiness return value',
                    'witness': 'One active VERIFIED document and one ' + ('active' if self.active else 'inactive') + ' PENDING document',
                    'qualifiers': ('The non-VERIFIED document must be active and PENDING is a permitted state',),
                    'citations': refs(request, unit, 'PR_HEAD')})}) for issue in proposal.issues)})
        return edit_output(reply, transform)

    async def challenge(self, request, **kwargs):
        self.challenge_verdict = 'SUPPORTED' if self.active and not run(BEFORE, True) and run(self.head, True) else 'REFUTED'
        return await super().challenge(request, **kwargs)


@pytest.mark.parametrize('head,active,attribution,confirmed', [
    (AFTER, True, 'INTRODUCED', True),
    (AFTER, False, 'INTRODUCED', False),
    (SAFE, True, 'INTRODUCED', False),
    (AFTER, True, 'PRE_EXISTING', False),
])
async def test_claimiq_mixed_active_status_contract_via_google_wire(head, active, attribution, confirmed):
    context, source = await fixture(policy=RULE, files=[changed('service.py', BEFORE, head)])
    oracle = ClaimIQOracle(head, active, attribution)
    transport = llm()
    class CapturedModel(VertexEvaluationModel):
        async def _call(self, request, phase, max_output_tokens, timeout):
            raw = await (oracle.assess(request) if phase == 'ASSESS' else oracle.challenge(request))
            schema = AssessmentBatchOutput if phase == 'ASSESS' else ChallengeBatchOutput
            wire = compact(schema.model_validate_json(raw.output_json), request)
            transport.vertex_client.aio.models.generate_content.return_value = response(wire.model_dump_json())
            return await super()._call(request, phase, max_output_tokens, timeout)
    model = CapturedModel(transport, deployment_revision='frozen-google-fixture-v1')
    result = await evaluate(context, source, model)
    assert bool(any(f.disposition == 'SUPPORTED_CONFLICT' for f in result.findings)) == confirmed
    assert oracle.requests
    if attribution == 'PRE_EXISTING':
        assert not oracle.challenge_requests  # Existing attribution gate avoids unnecessary challenges.
    else:
        assert oracle.challenge_requests
    assert all(call.provider == 'google' for call in result.calls)
    if confirmed:
        assert result.findings[0].core.qualifiers == ('The non-VERIFIED document must be active and PENDING is a permitted state',)


async def test_reverted_equality_guard_does_not_create_a_fresh_conflict():
    context, source = await fixture(policy=RULE, files=[changed('service.py', BEFORE, BEFORE)])
    result = await evaluate(context, source, ScriptedModel())
    assert not any(f.disposition == 'SUPPORTED_CONFLICT' for f in result.findings)
