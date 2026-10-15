"""Response contracts independent of repository semantics or live providers."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_services.decision_guard.evaluation import AzureEvaluationModel
from ai_services.decision_guard.evaluation.evidence import resolve_quote
from ai_services.decision_guard.evaluation.models import AssessmentBatchOutput, QuoteRef
from ai_services.decision_guard.evaluation.validation import validate_support
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    compact,
)
from ai_services.decision_guard.models import EvidenceSnapshot, digest
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    evaluate,
    fixture,
)


def data():
    return {
        "batch_id": "batch",
        "quotes": [
            {"quote_id": "H1", "source_ref": "PR_HEAD:1", "quote": "return value"}
        ],
        "units": [
            {
                "unit_id": "unit",
                "applicability": "APPLIES",
                "normative": "DESCRIPTIVE",
                "lifecycle": "CURRENT_OR_UNSPECIFIED",
                "conclusion": "NO_CONFLICT",
                "explanation": "Supported local behavior",
                "support": ["H1"],
                "observation_ids": [],
                "issues": [],
                "needs": [],
            }
        ],
    }


def parse(value):
    return WireAssessment.model_validate_json(json.dumps(value))


class OfflineClient:
    def __init__(self, output, *, refusal=None, finish="stop"):
        self.output, self.refusal, self.finish = output, refusal, finish
        self.calls = 0
        self.beta = self.chat = self.completions = self

    def with_options(self, **kwargs):
        assert kwargs["max_retries"] == 0
        return self

    async def parse(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            id="offline",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[
                SimpleNamespace(
                    finish_reason=self.finish,
                    message=SimpleNamespace(parsed=self.output, refusal=self.refusal),
                )
            ],
        )


def adapter(client):
    return AzureEvaluationModel(
        SimpleNamespace(client=client, deployment="offline"),
        deployment_revision="offline",
    )


def request_stub():
    return SimpleNamespace(
        model_dump=lambda **k: {
            "revision": {
                "repository_id": "repo",
                "base_sha": "a" * 40,
                "head_sha": "a" * 40,
                "merge_base_sha": "a" * 40,
            },
            "units": [],
            "evidence": [source().model_dump(mode="json")],
            "context_records_json": json.dumps(
                {"entities": [], "relationships": [], "requirements": []}
            ),
            "limitations": [],
            "adr_retrieval": "ABSENT",
            "adr_catalog_complete": True,
        },
        limitations=(),
        units=(),
        evidence=(source(),),
        revision=SimpleNamespace(
            repository_id="repo",
            base_sha="a" * 40,
            head_sha="a" * 40,
            merge_base_sha="a" * 40,
        ),
        adr_retrieval="ABSENT",
        adr_catalog_complete=True,
    )


@pytest.mark.parametrize(
    "unused",
    [
        {
            "quote_id": "H2",
            "source_ref": "PR_HEAD:9999",
            "quote": "unverified contradictory assertion",
        },
        {
            "quote_id": "H2",
            "source_ref": "PR_HEAD:1",
            "quote": "not in original source",
        },
    ],
)
async def test_unused_well_formed_quote_has_no_proof_credit(unused):
    value = data()
    value["quotes"].append(unused)
    wire = parse(value)
    expanded, ignored = wire.expanded_with_diagnostics(request_stub())
    assert ignored == ("H2",) and expanded.units[0].support == (
        QuoteRef(evidence_id="head", quote="return value"),
    )
    client = OfflineClient(wire)
    reply = await adapter(client).assess(
        request_stub(), max_output_tokens=5000, timeout=1
    )
    assert reply.status == "OK" and client.calls == 1
    assert reply.response_diagnostics[0].quote_ids == ("H2",)
    assert reply.wire_output_json == wire.model_dump_json()
    assert "H2" not in json.loads(reply.output_json)["units"][0]["support"]


@pytest.mark.parametrize(
    "mode", ["duplicate_id", "duplicate_pair", "unknown_reference"]
)
async def test_ambiguous_or_unbound_table_remains_invalid(mode):
    value = data()
    if mode == "duplicate_id":
        value["quotes"].append({**value["quotes"][0], "quote": "different"})
    elif mode == "duplicate_pair":
        value["quotes"].append({**value["quotes"][0], "quote_id": "H2"})
    else:
        value["units"][0]["support"] = ["H9999"]
    reply = await adapter(OfflineClient(parse(value))).assess(
        request_stub(), max_output_tokens=5000, timeout=1
    )
    assert reply.status == "INVALID" and reply.output_json is None
    assert reply.response_diagnostics[0].code in {
        "DUPLICATE_QUOTE_DEFINITION",
        "UNKNOWN_QUOTE_ID",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"quote_id": "H3", "source_ref": "PR_HEAD:1", "quote": ""},
        {"quote_id": "H3", "source_ref": "PR_HEAD:1", "quote": "x" * 1001},
        {"quote_id": "bad!", "source_ref": "PR_HEAD:1", "quote": "text"},
    ],
)
def test_unused_malformed_definition_still_fails_wire_schema(bad):
    value = data()
    value["quotes"].append(bad)
    with pytest.raises(ValidationError):
        parse(value)


def test_quote_count_and_diagnostics_are_bounded():
    value = data()
    value["quotes"] += [
        {"quote_id": f"H{i + 2}", "source_ref": "PR_HEAD:1", "quote": str(i)}
        for i in range(127)
    ]
    assert len(parse(value).expanded_with_diagnostics(request_stub())[1]) == 127
    value["quotes"].append(
        {"quote_id": "H9998", "source_ref": "PR_HEAD:1", "quote": "overflow"}
    )
    with pytest.raises(ValidationError):
        parse(value)


def test_empty_table_is_not_automatic_clean_proof():
    value = data()
    value["quotes"] = []
    value["units"][0]["support"] = []
    assert not parse(value).expanded(request_stub()).units[0].support
    value["units"][0]["support"] = ["H1"]
    with pytest.raises(ValueError, match="UNKNOWN_QUOTE_ID"):
        parse(value).expanded(request_stub())


def source(eid="head", role="PR_HEAD", text='`Field` = "cafÃƒÂ©"\n  return value\t'):
    return EvidenceSnapshot(
        evidence_id=eid,
        repository_id="repo",
        kind="CODE",
        origin=role,
        path="module.py",
        commit_sha="a" * 40,
        excerpt=text,
        excerpt_hash=digest(text),
        content_hash=digest(text),
        completeness="COMPLETE",
        placement="EXACT",
    )


@pytest.mark.parametrize("text", ["`Field`", '"cafÃƒÂ©"', "\n  return value\t"])
def test_literal_unicode_punctuation_and_whitespace_are_preserved(text):
    e = source()
    c = resolve_quote(
        QuoteRef(evidence_id=e.evidence_id, quote=text), {e.evidence_id: e}
    )
    assert c.quote == text and e.excerpt[c.start_char : c.end_char] == text


@pytest.mark.parametrize(
    "text", ['Field = "cafÃƒÂ©"', '"cafe"', "\n return value\t", "return value "]
)
def test_inexact_referenced_quotes_are_never_repaired(text):
    with pytest.raises(ValueError, match="QUOTE_NOT_FOUND"):
        resolve_quote(QuoteRef(evidence_id="head", quote=text), {"head": source()})


def test_unknown_evidence_fails_source_validation():
    with pytest.raises(ValueError, match="UNKNOWN_EVIDENCE"):
        resolve_quote(
            QuoteRef(evidence_id="foreign", quote="return value"), {"head": source()}
        )


@pytest.mark.parametrize(
    "role", ["MERGE_BASE", "TARGET_BASE", "PR_HEAD", "INDEXED_CODE"]
)
def test_identical_text_does_not_transfer_revision_authority(role):
    e = source("selected", role)
    unit = SimpleNamespace(
        unit_id="unit", evidence_ids=("selected",), path="module.py", entity_ids=()
    )
    req = SimpleNamespace(
        evidence=(e,),
        observations=(),
        context_records_json='{"entities":[]}',
        revision=SimpleNamespace(
            repository_id="repo",
            merge_base_sha=e.commit_sha,
            base_sha=e.commit_sha,
            head_sha=e.commit_sha,
        ),
    )
    refs = (QuoteRef(evidence_id="selected", quote="return value"),)
    if role == "PR_HEAD":
        assert validate_support(refs, (), req, unit, role="PR_HEAD")[0]
    else:
        with pytest.raises(ValueError, match="WRONG_REVISION_CITATION"):
            validate_support(refs, (), req, unit, role="PR_HEAD")


def test_challenge_unused_quotes_have_no_review_credit():
    wire = WireChallenge(batch_id="b", spans=(), decisions=())
    value = wire.model_dump(mode="json")
    value["spans"] = [
        {"span_id": "H1", "source_ref": "PR_HEAD:1", "start_line": 1, "end_line": 1}
    ]
    assert (
        not WireChallenge.model_validate_json(json.dumps(value))
        .expanded(SimpleNamespace(assessment=request_stub()))
        .decisions
    )


class ContractModel(ScriptedModel):
    def __init__(self, mutate):
        super().__init__()
        self.mutate = mutate

    async def assess(self, request, **kwargs):
        reply = await super().assess(request, **kwargs)
        wire = compact(
            AssessmentBatchOutput.model_validate_json(reply.output_json), request
        ).model_dump(mode="json")
        self.mutate(wire)
        return await adapter(OfflineClient(parse(wire))).assess(
            request, max_output_tokens=5000, timeout=1
        )


async def test_independent_unit_source_failure_does_not_lose_valid_unit():
    ctx, src = await fixture()

    def mutate(w):
        w["quotes"].append(
            {"quote_id": "H3", "source_ref": "PR_HEAD:9999", "quote": "fabricated"}
        )
        w["units"][0]["support"] = ["H3"]

    model = ContractModel(mutate)
    result = await evaluate(ctx, src, model)
    assert (
        len(result.units) == 2
        and result.coverage.uncertain == 1
        and result.coverage.assessed == 1
    )
    assert (
        result.outcome == "REVIEW_REQUIRED"
        and not model.challenge_requests
        and not result.findings
    )


@pytest.mark.parametrize(
    "mode", ["missing", "extra", "duplicate", "unknown_quote", "empty_support"]
)
async def test_no_conflict_cannot_hide_failed_or_unexpected_unit(mode):
    ctx, src = await fixture()

    def mutate(w):
        if mode == "missing":
            w["units"].pop()
        elif mode == "extra":
            w["units"].append({**w["units"][0], "unit_id": "not-assigned"})
        elif mode == "duplicate":
            w["units"].append(w["units"][0])
        elif mode == "unknown_quote":
            w["units"][0]["support"] = ["H9999"]
        else:
            w["units"][0]["support"] = []

    result = await evaluate(ctx, src, ContractModel(mutate))
    assert len(result.units) == 2 and not result.findings
    if mode == "empty_support":
        assert result.coverage.uncertain == 1 and result.coverage.assessed == 1
        assert result.outcome == "REVIEW_REQUIRED"
    else:
        assert result.coverage.errors == 2 and result.coverage.assessed == 0
        assert result.outcome == "NOT_EVALUATED"


async def test_unused_metadata_does_not_resolve_architectural_uncertainty():
    ctx, src = await fixture()

    def mutate(w):
        w["quotes"].append(
            {
                "quote_id": "H9998",
                "source_ref": "PR_HEAD:9999",
                "quote": "No exceptions anywhere",
            }
        )
        w["units"][0]["conclusion"] = "INSUFFICIENT"
        w["units"][0]["applicability"] = "UNCERTAIN"

    result = await evaluate(ctx, src, ContractModel(mutate))
    assert (
        result.outcome == "REVIEW_REQUIRED"
        and result.coverage.uncertain >= 1
        and not result.findings
    )


async def test_two_issues_in_one_unit_remain_fail_closed_without_losing_other_unit():
    ctx, src = await fixture()

    def mutate(w):
        issue = json.loads(json.dumps(w["units"][0]["issues"][0]))
        issue["claim"] = "Independent candidate with an invalid source citation"
        w["quotes"].append(
            {"quote_id": "H3", "source_ref": "PR_HEAD:9999", "quote": "invented"}
        )
        issue["core"]["citations"] = ["H3"]
        w["units"][0]["issues"].append(issue)

    model = ContractModel(mutate)
    model.outcomes = {("ADR", "service.py"): "conflict"}
    result = await evaluate(ctx, src, model)
    assert (
        len(result.units) == 2
        and result.coverage.uncertain == 1
        and result.coverage.assessed == 1
    )
    assert len(result.findings) == 1 and model.challenge_requests
    assert result.findings[0].disposition == "SUPPORTED_CONFLICT"
    assert len(result.rejected_findings) == 1
    assert result.outcome == "CONFLICTS_FOUND"


@pytest.mark.parametrize(
    "body",
    ["{broken json", '{"batch_id":"b","quotes":[],"units":[{"unexpected":true}]}'],
)
async def test_raw_decode_or_wire_schema_failure_is_not_an_architectural_verdict(body):
    class BrokenClient(OfflineClient):
        async def parse(self, **kwargs):
            return WireAssessment.model_validate_json(body)

    with pytest.raises(ValidationError):
        await adapter(BrokenClient(None)).assess(
            request_stub(), max_output_tokens=5000, timeout=1
        )


def test_historical_case_a_contract_is_not_silently_repaired():
    from ai_services.decision_guard.evaluation.historical_models import (
        AssessmentBatchOutput as HistoricalOutput,
    )
    from ai_services.runs.prepare_stage_4_2_live_validation import ROOT

    paths = [ROOT / "call_03.provider.json", ROOT / "call_04.reply.json"]
    if not all(p.exists() for p in paths):
        pytest.skip("Private historical archive unavailable")
    original = [p.read_bytes() for p in paths]
    raw = json.loads(original[0])
    with pytest.raises(ValidationError):
        WireAssessment.model_validate_json(raw["choices"][0]["message"]["content"])
    reply = json.loads(original[1])
    HistoricalOutput.model_validate_json(reply["output_json"])
    with pytest.raises(ValidationError):
        AssessmentBatchOutput.model_validate_json(reply["output_json"])
    assert [p.read_bytes() for p in paths] == original
