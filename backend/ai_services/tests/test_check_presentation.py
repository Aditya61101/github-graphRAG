"""Offline UX regression tests; Case A evidence is never rewritten."""

# ruff: noqa: F811 -- shared pytest fixtures are injected by name.

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from markdown_it import MarkdownIt

from ai_services.decision_guard.evaluation.models import EvaluationResult
from ai_services.decision_guard.orchestration.check_presentation import (
    SUMMARY_BYTES,
    code_link,
    details_url,
    prose,
    render_review,
)
from ai_services.decision_guard.orchestration.checks import (
    CheckPublisher,
    check_payload,
)
from ai_services.decision_guard.orchestration.read_models import CheckPublication
from ai_services.tests.test_frontend_checks import evaluated, github  # noqa: F401
from ai_services.tests.test_pr_orchestration import env  # noqa: F401


@pytest.fixture
def case_a():
    path = (
        Path(__file__).resolve().parents[2]
        / "data/stage_4_3_live/approved_full_run_result.json"
    )
    if not path.exists():
        pytest.skip("Real local Case A evidence is unavailable")
    return EvaluationResult.model_validate_json(path.read_text(encoding="utf-8-sig"))


def test_case_a_concise_canonical_evidence_only(case_a):
    before = case_a.model_dump_json()
    output = render_review(case_a, "Aditya61101/claimIQ")
    text = output["summary"]
    for heading in (
        "## Architectural review",
        "### Review summary",
        "### Confirmed architectural conflicts",
        "### Additional review required",
        "### Coverage and limitations",
    ):
        assert heading in text
    assert "**Supported governing-rule findings:** 3" in text
    assert "**Additional findings requiring review:** 1" in text
    assert "3 assessed · 6 uncertain · 0 omitted · 0 execution errors" in text
    assert "3 rejected proof records" in text and "not confirmed conflicts" in text
    assert "Required quotations could not be matched" in text
    assert "not necessarily distinct bugs" in text
    assert "VERIFIED" in text and "every associated active document" in text
    assert "independent review could not validate" in text
    assert "View changed code" in text
    assert "no public link" in text and "View governing ADR" not in text
    for finding in case_a.findings:
        assert finding.finding_id not in text
    for code in (
        "WITNESS_ORIGINAL_SOURCE_NOT_REVIEWED",
        "CONSTRAINT_LIMIT",
        "QUOTE_NOT_FOUND",
        "adr_917ed0fc62a7",
    ):
        assert code not in text
    # URLs retain immutable SHAs, but visible labels/prose contain no long IDs.
    visible = re.sub(r"\]\(https://[^)]+\)", "]", text)
    assert not re.search(r"[a-f0-9]{32,}", visible)
    assert len(text.split()) < 650
    assert case_a.model_dump_json() == before


def test_no_grouping_or_claim_equivalence_from_similar_text(case_a):
    text = render_review(case_a, "Aditya61101/claimIQ")["summary"]
    assert text.count("[View changed code]") == 3
    assert "same bug" not in text and "identical" not in text
    assert "same recorded code change" in text
    separate = case_a.model_copy(
        update={
            "plan": tuple(
                u.model_copy(update={"delta_ids": (u.unit_id + "_delta",)})
                for u in case_a.plan
            )
        }
    )
    assert (
        "same recorded code change"
        not in render_review(separate, "Aditya61101/claimIQ")["summary"]
    )
    # Each confirmed rule's distinct wording survives, even for overlapping code.
    assert "invoke\\_claim\\_graph" in text
    assert "assigns this aggregation decision" in text


def test_complete_partial_and_operational_presentations(case_a):
    clean = case_a.model_copy(
        update={
            "findings": (),
            "rejected_findings": (),
            "limitations": (),
            "outcome": "NO_CONFLICTS_FOUND",
            "coverage": case_a.coverage.model_copy(
                update={"scope": "COMPLETE_FOR_RETAINED_SCOPE", "uncertain": 0}
            ),
        }
    )
    text = render_review(clean, "Aditya61101/claimIQ")["summary"]
    assert "No confirmed conflict" in text and "Complete for the retained scope" in text
    partial = clean.model_copy(update={"coverage": case_a.coverage})
    assert "review required" in render_review(partial, "Aditya61101/claimIQ")["summary"]
    assert (
        "Evaluation unavailable"
        in render_review(None, "Aditya61101/claimIQ")["summary"]
    )


@pytest.mark.parametrize(
    "attack",
    [
        "[click](https://evil.example)",
        "<img src=x onerror=alert(1)>",
        "\n# Injected heading\n@everyone",
        "https://evil.example",
        "![x](data:text/html,attack)",
        "` **_strike_** | table",
    ],
)
def test_markdown_prose_cannot_introduce_links_html_mentions_or_blocks(attack):
    text = prose(attack)
    assert "\n" not in text and "<img" not in text and "@everyone" not in text
    assert "](https:" not in text and "https://" not in text and "data:text" not in text
    assert not text.startswith("#")
    rendered = MarkdownIt("commonmark").render(text)
    assert "<a " not in rendered and "<img" not in rendered and "<h1" not in rendered


@pytest.mark.parametrize(
    "base",
    [
        "http://localhost:5173",
        "https://localhost",
        "https://127.0.0.2",
        "https://[::1]",
        "https://[::ffff:127.0.0.1]",
        "https://10.0.0.1",
        "https://2130706433",
        "https://test.localhost",
        "https://user:secret@example.com",
        "https://example.com/?x=1",
        "https://example.com/#fragment",
        "https://example.com/../x",
        "https://example.com/%2e%2e/x",
        "https://example.com/projects/wrong/pull-requests/5",
    ],
)
def test_invalid_details_urls_are_rejected(base):
    with pytest.raises(ValueError):
        details_url(base, "repo_101", 2)


def test_details_scope_and_new_check_omission(case_a):
    assert details_url(None, "repo_101", 2) is None
    assert (
        details_url("https://decisionguard.example/app", "repo_101", 2)
        == "https://decisionguard.example/app/projects/repo_101/pull-requests/2"
    )
    job = SimpleNamespace(
        id="preview",
        head_sha=case_a.revision.head_sha,
        repository_id=case_a.revision.repository_id,
        pr_number=2,
    )
    assert "details_url" not in check_payload(job, case_a, "Aditya61101/claimIQ")


def test_verified_code_and_adr_location_links_only(case_a):
    finding = next(f for f in case_a.findings if f.disposition == "SUPPORTED_CONFLICT")
    loc = finding.head_location
    assert case_a.revision.head_sha in code_link(
        "Aditya61101/claimIQ", case_a.revision.head_sha, loc
    )
    assert code_link("other/repo?bad", case_a.revision.head_sha, loc) is None
    assert code_link("owner/repo", "main", loc) is None
    for path in ("../secret.py", "/etc/passwd", "a\\b", "a\nfile"):
        assert (
            code_link(
                "owner/repo",
                case_a.revision.head_sha,
                SimpleNamespace(path=path, start_line=1, end_line=2),
            )
            is None
        )
    # No head location means no invented line links, even with a similar claim.
    no_location = case_a.model_copy(
        update={
            "findings": tuple(
                f.model_copy(update={"head_location": None}) for f in case_a.findings
            )
        }
    )
    assert (
        "View changed code"
        not in render_review(no_location, "Aditya61101/claimIQ")["summary"]
    )


def test_original_adr_link_requires_revision_and_exact_location(case_a):
    # Synthetic metadata copies test presentation only; no stored record changes.
    from ai_services.decision_guard.models import SourceLocation

    location = SourceLocation(path="docs/readiness.md", start_line=12, end_line=16)
    evidence = tuple(
        e.model_copy(
            update={
                "location": location,
                "path": location.path,
                "commit_sha": case_a.revision.base_sha,
                "placement": "EXACT",
            }
        )
        if e.kind == "ADR"
        else e
        for e in case_a.evidence
    )
    linked = case_a.model_copy(update={"evidence": evidence})
    text = render_review(linked, "Aditya61101/claimIQ")["summary"]
    assert (
        f"[View governing ADR](https://github.com/Aditya61101/claimIQ/blob/{case_a.revision.base_sha}/docs/readiness.md#L12-L16)"
        in text
    )
    unknown_location = linked.model_copy(
        update={
            "evidence": tuple(
                e.model_copy(
                    update={
                        "placement": "UNLOCATED",
                        "source_url": "https://evil.example/fake-adr",
                    }
                )
                if e.kind == "ADR"
                else e
                for e in evidence
            )
        }
    )
    assert (
        "View governing ADR"
        not in render_review(unknown_location, "Aditya61101/claimIQ")["summary"]
    )
    assert (
        "evil.example"
        not in render_review(unknown_location, "Aditya61101/claimIQ")["summary"]
    )


def test_large_results_truncate_whole_entries_without_losing_counts_or_coverage(case_a):
    sample = next(f for f in case_a.findings if f.disposition == "SUPPORTED_CONFLICT")
    big = case_a.model_copy(
        update={
            "findings": tuple(
                sample.model_copy(
                    update={"claim": "界" * 10000, "suggested_action": "界" * 10000}
                )
                for _ in range(1000)
            )
        }
    )
    text = render_review(big, "Aditya61101/claimIQ")["summary"]
    assert len(text.encode()) <= SUMMARY_BYTES
    assert "**Supported governing-rule findings:** 1000" in text
    assert "Showing 6 of 1000" in text and "### Coverage and limitations" in text
    assert text.count("[View changed code](") == text.count("#L35-L62)")


@pytest.mark.asyncio
async def test_required_existing_check_never_creates_replacement(env, github):
    job_id = await evaluated(env)
    await CheckPublisher(
        env.store, github.app, enabled=True, required_check_id="100"
    ).publish(job_id)
    assert not github.writes
    with env.sessions() as session:
        assert session.get(CheckPublication, job_id).status == "BLOCKED"


@pytest.mark.asyncio
async def test_existing_patch_idempotent_and_omission_does_not_claim_to_clear_fallback(
    env, github
):
    job_id = await evaluated(env)
    pub = CheckPublisher(env.store, github.app, enabled=True)
    await pub.publish(job_id)
    github.runs[0]["details_url"] = "http://localhost:5173"
    before = env.store.get(job_id).provider_attempts
    pub = CheckPublisher(env.store, github.app, enabled=True, required_check_id="100")
    for _ in range(2):
        pub.retry(job_id)
        await pub.publish(job_id)
    assert [w[0] for w in github.writes] == ["POST", "PATCH", "PATCH"]
    assert len(github.runs) == 1
    assert all("details_url" not in w[2] for w in github.writes)
    assert github.writes[-1][2] == github.writes[-2][2]
    assert env.store.get(job_id).provider_attempts == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "d" * 40),
        ("app", {"id": 99}),
        ("url", "https://api.github.com/repos/wrong/repo/check-runs/100"),
        ("external_id", "another-job"),
    ],
)
async def test_remote_identity_mismatch_blocks_patch(env, github, field, value):
    job_id = await evaluated(env)
    pub = CheckPublisher(env.store, github.app, enabled=True)
    await pub.publish(job_id)
    github.runs[0][field] = value
    pub.retry(job_id)
    await pub.publish(job_id)
    assert len(github.writes) == 1
    with env.sessions() as session:
        assert session.get(CheckPublication, job_id).status == "BLOCKED"
