"""Source/relationship validation only. Semantic sufficiency belongs to review."""

import json

from ..models import digest, stable_id
from .evidence import resolve_quote
from .models import ProofRecord


def source_anchors(request, unit):
    """Authorize reads only from backend-produced source identities in this unit."""
    anchors = {
        e.evidence_id: e.path
        for e in request.evidence
        if e.path and e.kind == "CODE" and e.evidence_id in unit.evidence_ids
    }
    anchors.update(
        {
            a.anchor_id: a.path
            for a in request.source_anchors
            if unit.unit_id in a.unit_ids
        }
    )
    return anchors


def validate_missing_facts(facts, request, unit):
    anchors = source_anchors(request, unit)
    for fact in facts:
        if fact.anchor_id and fact.anchor_id not in anchors:
            raise ValueError("UNAUTHORIZED_FACT_ANCHOR")


def validate_support(refs, observation_ids, request, unit, *, role=None):
    evidence = {
        e.evidence_id: e for e in request.evidence if e.evidence_id in unit.evidence_ids
    }
    roles = {
        "MERGE_BASE": request.revision.merge_base_sha,
        "TARGET_BASE": request.revision.base_sha,
        "PR_HEAD": request.revision.head_sha,
    }
    for e in evidence.values():
        if (
            e.repository_id != request.revision.repository_id
            or e.excerpt_hash != digest(e.excerpt)
        ):
            raise ValueError("INVALID_EVIDENCE_IDENTITY")
        if e.kind == "CODE" and e.origin in roles and e.commit_sha != roles[e.origin]:
            raise ValueError("WRONG_SOURCE_REVISION")
    citations = tuple(resolve_quote(r, evidence) for r in refs)
    if role and any(
        evidence[c.evidence_id].kind != "CODE" or evidence[c.evidence_id].origin != role
        for c in citations
    ):
        raise ValueError("WRONG_REVISION_CITATION")
    observations = {o.observation_id: o for o in request.observations}
    selected = []
    for oid in observation_ids:
        o = observations.get(oid)
        if o is None or not o.complete_file or o.status == "UNAVAILABLE":
            raise ValueError("INVALID_SOURCE_OBSERVATION")
        if role and o.revision != role:
            raise ValueError("WRONG_REVISION_OBSERVATION")
        if o.status == "PRESENT":
            e = evidence.get(o.evidence_id)
            if (
                e is None
                or e.kind != "CODE"
                or (e.path, e.origin) != (o.path, o.revision)
                or e.completeness != "COMPLETE"
            ):
                raise ValueError("OBSERVATION_OUTSIDE_UNIT")
        elif o.path != unit.path and not any(
            e.path == o.path for e in evidence.values()
        ):
            raise ValueError("OBSERVATION_OUTSIDE_UNIT")
        selected.append(o)
    return citations, selected


def defining_adrs(request, unit):
    rule = next(
        (
            r
            for r in json.loads(request.context_records_json)["requirements"]
            if r["requirement_id"] == unit.rule_id
        ),
        None,
    )
    return set(rule["adr_ids"]) if rule else set()


def authority_established(request, unit):
    ids = defining_adrs(request, unit)
    receipts = {a.adr_id: a for a in request.authority}
    return bool(ids) and all(
        a in receipts
        and receipts[a].status == "APPROVED"
        and receipts[a].repository_id == request.revision.repository_id
        for a in ids
    )


def validate_rule(citations, request, unit):
    ev = {e.evidence_id: e for e in request.evidence}
    if unit.channel == "RKG":
        if not any(ev[c.evidence_id].kind == "CODE" for c in citations):
            raise ValueError("ORIGINAL_CODE_RULE_REQUIRED")
        return
    owned = defining_adrs(request, unit)
    receipts = {a.adr_id: a for a in request.authority}
    if not any(
        ev[c.evidence_id].kind == "ADR" and ev[c.evidence_id].adr_id in owned
        for c in citations
    ):
        raise ValueError("ORIGINAL_ADR_RULE_REQUIRED")
    for c in citations:
        e = ev[c.evidence_id]
        if e.kind == "ADR" and (
            e.adr_id not in owned
            or (
                e.adr_id in receipts
                and receipts[e.adr_id].document_hash != e.document_hash
            )
        ):
            raise ValueError("FOREIGN_POLICY_EVIDENCE")


def source_coverage(request, unit, *, revisions=True):
    roles = ("MERGE_BASE", "TARGET_BASE", "PR_HEAD") if revisions else ("PR_HEAD",)
    for role in roles:
        obs = [
            o
            for o in request.observations
            if o.path == unit.path
            and o.revision == role
            and o.complete_file
            and o.status in {"PRESENT", "ABSENT"}
        ]
        if not obs:
            return False
        try:
            validate_support(
                (), tuple(o.observation_id for o in obs), request, unit, role=role
            )
        except ValueError:
            return False
    if unit.channel == "ADR":
        owned = defining_adrs(request, unit)
        complete = {
            o.adr_id
            for o in request.policy_observations
            if o.coverage == "TEXT_COMPLETE"
            and o.evidence_id in unit.evidence_ids
            and any(
                e.evidence_id == o.evidence_id
                and e.adr_id == o.adr_id
                and e.document_hash == o.document_hash
                and e.completeness == "COMPLETE"
                for e in request.evidence
            )
        }
        if not owned or not owned <= complete:
            return False
    return True


def validate_proposal(proposal, request, unit):
    if proposal.unit_id != unit.unit_id:
        raise ValueError("WRONG_ASSESSMENT_UNIT")
    facts = {f.fact_id: f for issue in proposal.issues for f in issue.missing_facts}
    anchors = source_anchors(request, unit)
    for need in proposal.needs:
        if need.anchor_id not in anchors:
            raise ValueError("UNAUTHORIZED_EVIDENCE_NEED")
        if need.fact_id and (
            need.fact_id not in facts
            or (facts[need.fact_id].anchor_id, facts[need.fact_id].revision)
            != (need.anchor_id, need.revision)
        ):
            raise ValueError("EVIDENCE_NEED_FACT_MISMATCH")
    citations, _ = validate_support(
        proposal.support, proposal.observation_ids, request, unit
    )
    # Issue evidence is independent; unit-level support is required for clean exclusions.
    if not proposal.issues and not proposal.rejections:
        if not citations:
            raise ValueError("CLEAN_RESULT_NEEDS_SUPPORT")
        validate_rule(citations, request, unit)
        if (
            proposal.conclusion == "NOT_APPLICABLE"
            and proposal.applicability not in {"DOES_NOT_APPLY", "EXEMPT"}
            and proposal.lifecycle not in {"PROPOSED", "REJECTED", "SUPERSEDED"}
        ):
            raise ValueError("EXCLUSION_NOT_ESTABLISHED")
        if (
            proposal.conclusion == "NO_CONFLICT"
            and proposal.applicability == "UNCERTAIN"
        ):
            raise ValueError("UNCERTAIN_CANNOT_PASS")
        if proposal.conclusion == "NO_CONFLICT":
            ev = {e.evidence_id: e for e in request.evidence}
            if not any(
                ev[c.evidence_id].kind == "CODE"
                and ev[c.evidence_id].origin == "PR_HEAD"
                for c in citations
            ) and not any(
                o.path == unit.path
                and o.revision == "PR_HEAD"
                and o.status == "ABSENT"
                and o.complete_file
                for o in request.observations
            ):
                raise ValueError("CLEAN_RESULT_NEEDS_HEAD_SOURCE")
        if proposal.conclusion == "NO_CONFLICT" and not source_coverage(request, unit):
            raise ValueError("CLEAN_RESULT_NEEDS_SOURCE_COVERAGE")
    return citations


def revision_behavior(rows, request, unit, attribution):
    citations = []
    behaviors = {}
    for row in rows:
        cs, obs = validate_support(
            row.citations, row.observation_ids, request, unit, role=row.revision
        )
        citations.extend(cs)
        if any(
            next(
                e for e in request.evidence if e.evidence_id == c.evidence_id
            ).placement
            != "EXACT"
            for c in cs
        ):
            raise ValueError("INEXACT_REVISION_SOURCE")
        behaviors[row.revision] = row.behavior if cs or obs else "UNKNOWN"
    before, head, target = (
        behaviors[k] for k in ("MERGE_BASE", "PR_HEAD", "TARGET_BASE")
    )
    transition = {
        "INTRODUCED": before in {"SATISFIES", "NOT_APPLICABLE"}
        and head == "VIOLATES"
        and target in {"SATISFIES", "NOT_APPLICABLE"},
        "WORSENED": before == head == "VIOLATES" and target != "UNKNOWN",
        "PRE_EXISTING": before == head == "VIOLATES" and target != "UNKNOWN",
        "RESOLVED": before == "VIOLATES" and head == "SATISFIES",
        "UNCHANGED": before == head and head != "UNKNOWN",
        "UNKNOWN": True,
    }[attribution]
    deltas = [
        d
        for d in json.loads(request.context_records_json)["deltas"]
        if d["delta_id"] in unit.delta_ids
    ]
    changed = any(
        d["before"] != d["after"] or d["previous_path"] not in (None, d["path"])
        for d in deltas
    )
    return tuple(
        citations
    ), attribution if transition and target != "UNKNOWN" and changed else "UNKNOWN"


def validate_issue(proposal, issue, request, unit, key):
    expected = "ADR_CONFLICT" if unit.channel == "ADR" else "RKG_REGRESSION"
    if issue.kind not in {expected, "ARCHITECTURAL_IMPACT"}:
        raise ValueError("WRONG_FINDING_CHANNEL")
    validate_missing_facts(issue.missing_facts, request, unit)
    rule, _ = validate_support(issue.rule.citations, (), request, unit)
    validate_rule(rule, request, unit)
    core, obs = validate_support(
        issue.core.citations, issue.core.observation_ids, request, unit, role="PR_HEAD"
    )
    ev = {e.evidence_id: e for e in request.evidence}
    if not any(
        ev[c.evidence_id].kind == "CODE"
        and ev[c.evidence_id].origin == "PR_HEAD"
        and ev[c.evidence_id].path == unit.path
        and ev[c.evidence_id].placement == "EXACT"
        for c in core
    ) and not any(o.revision == "PR_HEAD" and o.path == unit.path for o in obs):
        raise ValueError("CORE_NEEDS_CHANGED_HEAD_SOURCE")
    counter, _ = validate_support(issue.counterevidence.citations, (), request, unit)
    revisions, attribution = revision_behavior(
        issue.revisions, request, unit, issue.attribution
    )
    citations = tuple(dict.fromkeys((*rule, *core, *counter, *revisions)))
    approved = unit.channel == "RKG" or authority_established(request, unit)
    eligible = bool(
        issue.kind != "ARCHITECTURAL_IMPACT"
        and approved
        and source_coverage(request, unit)
        and attribution in {"INTRODUCED", "WORSENED"}
        and proposal.applicability == "APPLIES"
        and (
            unit.channel == "RKG"
            or (
                proposal.normative == "MANDATORY"
                and proposal.lifecycle == "CURRENT_OR_UNSPECIFIED"
            )
        )
        and not any(n.fact_id is None for n in proposal.needs)
        and not any(f.scope == "CORE" for f in issue.missing_facts)
    )
    proof = ProofRecord(
        proof_id=stable_id(key, "source-validation"),
        unit_id=unit.unit_id,
        obligation="CANDIDATE_SOURCE_VALIDITY",
        supported=True,
        method="SOURCE_VALIDATION",
        explanation="Literal evidence, source roles and finding relationships verified; semantic sufficiency awaits independent review",
        citations=citations,
    )
    return (proof,), citations, attribution, eligible


def validate_review(decision, request, unit, key, candidate):
    if decision.proposal_id != key:
        raise ValueError("WRONG_REVIEW_CANDIDATE")
    validate_missing_facts(decision.missing_facts, request, unit)
    citations, observations = validate_support(
        decision.citations, decision.observation_ids, request, unit
    )
    revisions, attribution = revision_behavior(
        decision.revisions, request, unit, decision.attribution
    )
    if not citations:
        raise ValueError("REVIEW_NEEDS_ORIGINAL_EVIDENCE")
    if (
        decision.verdict in {"SUPPORTED", "REFUTED"}
        and decision.witness_correspondence != "UNCHANGED"
    ):
        raise ValueError("WITNESS_CORRESPONDENCE_" + decision.witness_correspondence)
    if decision.verdict == "SUPPORTED":
        # Source correspondence is necessary, not semantic equivalence. Never
        # compare witness prose or infer qualification from matching quotations.
        core, core_observations = validate_support(
            candidate.core.citations,
            candidate.core.observation_ids,
            request,
            unit,
            role="PR_HEAD",
        )
        if not {c.evidence_id for c in core} <= {
            c.evidence_id for c in citations
        } or not {o.observation_id for o in core_observations} <= {
            o.observation_id for o in observations
        }:
            raise ValueError("WITNESS_ORIGINAL_SOURCE_NOT_REVIEWED")
        validate_rule(citations, request, unit)
        if not source_coverage(request, unit):
            raise ValueError("REVIEW_NEEDS_COMPLETE_SOURCE")
        ev = {e.evidence_id: e for e in request.evidence}
        if not any(
            ev[c.evidence_id].kind == "CODE"
            and ev[c.evidence_id].origin == "PR_HEAD"
            and ev[c.evidence_id].path == unit.path
            for c in citations
        ) and not any(
            o.path == unit.path and o.revision == "PR_HEAD" and o.status == "ABSENT"
            for o in observations
        ):
            raise ValueError("REVIEW_NEEDS_HEAD_SUPPORT")
    accepted = not any(f.scope == "CORE" for f in decision.missing_facts) and (
        decision.verdict == "REFUTED"
        or (
            decision.verdict == "SUPPORTED"
            and attribution in {"INTRODUCED", "WORSENED"}
            and not any(f.scope == "CORE" for f in decision.missing_facts)
            and (unit.channel == "RKG" or authority_established(request, unit))
        )
    )
    return accepted, tuple(dict.fromkeys((*citations, *revisions))), attribution
