"""Read-only v1-v3 result/response schemas. Never used for new evaluation."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..models import (
    BaselineSnapshot,
    Contract,
    EvidenceSnapshot,
    Limitation,
    RevisionIdentity,
    SourceLocation,
)

ID = Annotated[str, Field(min_length=1)]
Text = Annotated[str, Field(max_length=2000)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Role = Literal["MERGE_BASE", "TARGET_BASE", "PR_HEAD"]
Attribution = Literal[
    "INTRODUCED", "WORSENED", "PRE_EXISTING", "RESOLVED", "UNCHANGED", "UNKNOWN"
]
Obligation = Literal[
    "NORMATIVE_RULE",
    "APPLICABILITY",
    "EXCEPTION_CLEARANCE",
    "HEAD_BEHAVIOR",
    "BASE_BEHAVIOR",
    "TARGET_COMPATIBILITY",
    "CAUSAL_DELTA",
    "IMPACT",
]
OBLIGATIONS = (
    "NORMATIVE_RULE",
    "APPLICABILITY",
    "EXCEPTION_CLEARANCE",
    "HEAD_BEHAVIOR",
    "BASE_BEHAVIOR",
    "TARGET_COMPATIBILITY",
    "CAUSAL_DELTA",
    "IMPACT",
)


class EvaluationSettings(Contract):
    profile: Literal["DEFAULT", "EXPANDED", "CUSTOM"] = "DEFAULT"
    max_attempts: int = Field(default=8, ge=1)
    max_tokens: int = Field(default=60000, ge=1)
    deadline_seconds: float = Field(default=120.0, gt=0)
    concurrency: int = Field(default=2, ge=1, le=16)
    call_timeout_seconds: float = Field(default=45.0, gt=0)
    max_input_tokens: int = Field(default=16000, ge=1)
    max_output_tokens: int = Field(default=5000, ge=1)
    model_window: int = Field(default=32768, ge=1)
    provider_reserve: int = Field(default=512, ge=0)
    max_batch_units: int = Field(default=12, ge=1, le=16)
    max_units: int = Field(default=4096, ge=1)
    max_reads: int = Field(default=96, ge=0)
    max_read_bytes: int = Field(default=65536, ge=1)
    max_source_bytes: int = Field(default=4194304, ge=1)
    max_policy_reads: int = Field(default=4, ge=0)
    max_policy_read_bytes: int = Field(default=65536, ge=1)
    max_policy_bytes: int = Field(default=262144, ge=1)
    retries: int = Field(default=1, ge=0, le=1)
    input_rate_per_million: float | None = Field(default=None, ge=0)
    output_rate_per_million: float | None = Field(default=None, ge=0)
    pricing_revision: str | None = None
    currency: str | None = None
    max_cost: float | None = Field(default=None, gt=0)

    @classmethod
    def expanded(cls, **overrides):
        return cls(
            **dict(
                {
                    "profile": "EXPANDED",
                    "max_attempts": 24,
                    "max_tokens": 240000,
                    "deadline_seconds": 300.0,
                    "concurrency": 3,
                },
                **overrides,
            )
        )

    @model_validator(mode="after")
    def valid(self):
        if self.max_output_tokens + self.provider_reserve >= self.model_window:
            raise ValueError("Model window cannot hold output and provider reserves")
        rates = (self.input_rate_per_million, self.output_rate_per_million)
        if any(v is not None for v in rates) and not all(v is not None for v in rates):
            raise ValueError("Both pricing rates are required")
        if (rates[0] is not None or self.max_cost is not None) and not (
            all(v is not None for v in rates)
            and self.pricing_revision
            and self.currency
        ):
            raise ValueError(
                "Cost accounting requires rates, currency and pricing revision"
            )
        return self


class QuoteRef(Contract):
    evidence_id: ID
    quote: Annotated[str, Field(min_length=1, max_length=1000)]


class Citation(QuoteRef):
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    location: SourceLocation | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.end_char - self.start_char != len(self.quote):
            raise ValueError("Citation range does not match quote")
        return self


class AuthorityReceipt(Contract):
    policy_version: Literal["repository-adr-v1"] = "repository-adr-v1"
    repository_id: ID
    adr_id: ID
    document_hash: Hash
    owner_id: ID
    status: Literal["APPROVED", "UNKNOWN"]
    reason: ID


class SourceObservation(Contract):
    observation_id: ID
    revision: Role
    path: ID
    status: Literal["PRESENT", "ABSENT", "UNAVAILABLE"]
    complete_file: bool
    evidence_id: str | None = None


class PolicyObservation(Contract):
    """Python-verified whole-document extraction, not ADR chunk completeness."""

    adr_id: ID
    document_hash: Hash
    evidence_id: ID
    coverage: Literal["TEXT_COMPLETE", "TEXT_PARTIAL"]


class EvidenceBundle(Contract):
    evidence: tuple[EvidenceSnapshot, ...] = ()
    observations: tuple[SourceObservation, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    policy_observations: tuple[PolicyObservation, ...] = ()


class EvaluationUnit(Contract):
    unit_id: ID
    channel: Literal["ADR", "RKG"]
    rule_id: ID
    path: ID
    delta_ids: tuple[ID, ...]
    entity_ids: tuple[ID, ...]
    relationship_ids: tuple[ID, ...]
    evidence_ids: tuple[ID, ...]


class ClearanceReview(Contract):
    """Bounded candidate-specific review; never a repository-wide absence claim."""

    basis: Literal[
        "EXPLICIT_RULE_PATH", "CHECKED_EXCEPTIONS_AND_SAFEGUARDS", "UNRESOLVED"
    ]
    scope: Annotated[str, Field(min_length=1, max_length=400)]
    checks: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        default=(), max_length=6
    )
    unresolved: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        default=(), max_length=6
    )


class SupportClaim(Contract):
    obligation: Obligation
    supported: bool
    explanation: Text
    citations: tuple[QuoteRef, ...] = Field(default=(), max_length=8)
    observation_ids: tuple[ID, ...] = Field(default=(), max_length=8)
    clearance: ClearanceReview | None = None


class RevisionAssessment(Contract):
    revision: Role
    behavior: Literal["SATISFIES", "VIOLATES", "NOT_APPLICABLE", "UNKNOWN"]
    citations: tuple[QuoteRef, ...] = Field(
        default=(),
        max_length=8,
        description="CODE evidence only, matching this exact revision; never ADR or graph evidence",
    )
    observation_ids: tuple[ID, ...] = Field(default=(), max_length=8)


class CoreAllegation(Contract):
    """Source-grounded boundary and qualifying witness; semantic proof stays LLM-owned."""

    boundary: Annotated[str, Field(min_length=1, max_length=400)]
    witness: Annotated[str, Field(min_length=1, max_length=600)]
    qualifiers: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        default=(), max_length=4
    )
    citations: tuple[QuoteRef, ...] = Field(default=(), max_length=8)
    observation_ids: tuple[ID, ...] = Field(default=(), max_length=8)


class MissingFact(Contract):
    fact_id: ID
    obligation: Obligation
    scope: Literal["CORE", "CONSEQUENCE"]
    fact: Annotated[str, Field(min_length=1, max_length=300)]
    relevance: Annotated[str, Field(min_length=1, max_length=300)]
    anchor_id: ID | None = None
    revision: Role = "PR_HEAD"
    unrequestable_reason: Annotated[str, Field(max_length=300)] | None = None

    @model_validator(mode="after")
    def actionable(self):
        if bool(self.anchor_id) == bool(self.unrequestable_reason):
            raise ValueError("Missing fact needs an anchor or an unrequestable reason")
        return self


class PotentialConsequence(Contract):
    """Explicitly conditional; never reported as an independently proven outcome."""

    claim: Annotated[str, Field(min_length=1, max_length=400)]
    condition: Annotated[str, Field(min_length=1, max_length=400)]


class ProposedIssue(Contract):
    kind: Literal["ADR_CONFLICT", "RKG_REGRESSION", "ARCHITECTURAL_IMPACT"]
    claim: Text
    mechanism: Text
    consequence: Text
    suggested_action: Text
    attribution: Attribution
    revisions: tuple[RevisionAssessment, ...] = Field(min_length=3, max_length=3)
    claims: tuple[SupportClaim, ...] = Field(max_length=8)
    core: CoreAllegation | None = None  # Historical v1/v2 responses remain readable.
    missing_facts: tuple[MissingFact, ...] = Field(default=(), max_length=4)
    potential_consequences: tuple[PotentialConsequence, ...] = Field(
        default=(), max_length=2
    )

    @model_validator(mode="after")
    def unique(self):
        if {r.revision for r in self.revisions} != {
            "MERGE_BASE",
            "TARGET_BASE",
            "PR_HEAD",
        }:
            raise ValueError("Exactly three distinct revision roles required")
        if len({c.obligation for c in self.claims}) != len(self.claims):
            raise ValueError("Duplicate proof obligation")
        if len({f.fact_id for f in self.missing_facts}) != len(self.missing_facts):
            raise ValueError("Duplicate missing fact")
        return self


class EvidenceNeed(Contract):
    anchor_id: ID
    revision: Role
    fact_id: ID | None = None


class UnitProposal(Contract):
    unit_id: ID
    applicability: Literal["APPLIES", "DOES_NOT_APPLY", "EXEMPT", "UNCERTAIN"]
    normative: Literal["MANDATORY", "ADVISORY", "DESCRIPTIVE", "UNKNOWN"]
    lifecycle: Literal[
        "CURRENT_OR_UNSPECIFIED", "PROPOSED", "REJECTED", "SUPERSEDED", "UNCERTAIN"
    ]
    conclusion: Literal["ISSUES", "NO_CONFLICT", "NOT_APPLICABLE", "INSUFFICIENT"]
    explanation: Text
    support: tuple[QuoteRef, ...] = Field(default=(), max_length=12)
    observation_ids: tuple[ID, ...] = Field(default=(), max_length=8)
    issues: tuple[ProposedIssue, ...] = Field(default=(), max_length=4)
    needs: tuple[EvidenceNeed, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def coherent(self):
        if (self.conclusion == "ISSUES") != bool(self.issues):
            raise ValueError("Only ISSUES contains issues")
        facts = [f.fact_id for issue in self.issues for f in issue.missing_facts]
        if len(set(facts)) != len(facts):
            raise ValueError("Missing fact IDs must be unique within a unit")
        return self


class AssessmentBatchOutput(Contract):
    batch_id: ID
    units: tuple[UnitProposal, ...] = Field(max_length=16)


class ChallengeDecision(Contract):
    proposal_id: ID
    verdict: Literal["SUPPORTED", "REFUTED", "INSUFFICIENT"]
    explanation: Text
    # Challenge must independently address each eligible claim, not just vote yes.
    claims: tuple[SupportClaim, ...] = Field(max_length=8)
    missing_facts: tuple[MissingFact, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def unique_facts(self):
        if len({f.fact_id for f in self.missing_facts}) != len(self.missing_facts):
            raise ValueError("Duplicate challenge missing fact")
        return self


class ChallengeBatchOutput(Contract):
    batch_id: ID
    decisions: tuple[ChallengeDecision, ...] = Field(max_length=16)


class SourceAnchor(Contract):
    anchor_id: ID
    path: ID
    unit_ids: tuple[ID, ...]


class AssessmentRequest(Contract):
    batch_id: ID
    context_hash: Hash
    revision: RevisionIdentity
    units: tuple[EvaluationUnit, ...]
    # Serialized selected Stage 4.1 records keep their existing versioned contracts.
    context_records_json: str
    evidence: tuple[EvidenceSnapshot, ...]
    observations: tuple[SourceObservation, ...]
    authority: tuple[AuthorityReceipt, ...]
    limitations: tuple[Limitation, ...]
    policy_observations: tuple[PolicyObservation, ...] = ()
    source_anchors: tuple[SourceAnchor, ...] = ()
    adr_retrieval: Literal["ABSENT", "AVAILABLE", "DEGRADED", "UNKNOWN"] = "UNKNOWN"
    adr_catalog_complete: bool = False


class ChallengeCandidate(Contract):
    proposal_id: ID
    unit_id: ID
    proposal: UnitProposal
    issue: ProposedIssue


class ChallengeRequest(Contract):
    batch_id: ID
    assessment: AssessmentRequest
    candidates: tuple[ChallengeCandidate, ...]


class ResponseDiagnostic(Contract):
    """Bounded response-contract audit metadata, never model input or proof."""

    stage: Literal["QUOTE_EXPANSION"]
    code: Annotated[str, Field(min_length=1, max_length=64)]
    quote_ids: tuple[Annotated[str, Field(max_length=32)], ...] = Field(
        default=(), max_length=128
    )


class ModelReply(Contract):
    output_json: str | None
    status: Literal["OK", "REFUSAL", "TRUNCATED", "INVALID", "ERROR"] = "OK"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    request_id: str | None = None
    wire_output_json: str | None = None
    response_diagnostics: tuple[ResponseDiagnostic, ...] = Field(
        default=(), max_length=8
    )


class CallRecord(Contract):
    batch_id: ID
    phase: Literal["ASSESS", "CHALLENGE"]
    status: ID
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    unreported_reserved_tokens: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    request_id: str | None = None


class ProofRecord(Contract):
    proof_id: ID
    unit_id: ID
    obligation: ID
    supported: bool
    method: Literal["SOURCE_VALIDATION", "SEMANTIC_ASSESSMENT", "SEMANTIC_CHALLENGE"]
    explanation: Text
    citations: tuple[Citation, ...] = ()
    observation_ids: tuple[ID, ...] = ()
    clearance: ClearanceReview | None = None


class Finding(Contract):
    finding_id: ID
    unit_id: ID
    kind: Literal["ADR_CONFLICT", "RKG_REGRESSION", "ARCHITECTURAL_IMPACT"]
    disposition: Literal[
        "SUPPORTED_CONFLICT", "ADVISORY", "NEEDS_REVIEW", "PRE_EXISTING"
    ]
    attribution: Attribution
    claim: Text
    consequence: Text
    suggested_action: Text
    core: CoreAllegation | None = None
    missing_facts: tuple[MissingFact, ...] = ()
    potential_consequences: tuple[PotentialConsequence, ...] = ()
    citations: tuple[Citation, ...]
    proof_ids: tuple[ID, ...]
    head_location: SourceLocation | None = None


class UnitDisposition(Contract):
    unit_id: ID
    status: Literal[
        "ASSESSED", "NOT_APPLICABLE", "EXEMPT", "UNCERTAIN", "BUDGET_OMITTED", "ERROR"
    ]
    reason_codes: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    proposal: UnitProposal | None = None


class EvaluationCoverage(Contract):
    assessed: int = Field(ge=0)
    uncertain: int = Field(ge=0)
    omitted: int = Field(ge=0)
    errors: int = Field(ge=0)
    scope: Literal["COMPLETE_FOR_RETAINED_SCOPE", "PARTIAL", "UNASSESSABLE"]


class EvaluationResult(Contract):
    schema_version: Literal["dg-evaluation-v1"] = "dg-evaluation-v1"
    engine_version: Literal["1", "2", "3"] = "3"
    evaluation_id: Hash
    context_hash: Hash
    bundle_hash: Hash
    settings_hash: Hash
    authority_hash: Hash
    revision: RevisionIdentity
    baseline: BaselineSnapshot
    deployment_revision: ID
    tokenizer: ID
    execution: Literal["COMPLETED", "PARTIAL"]
    outcome: Literal[
        "CONFLICTS_FOUND", "REVIEW_REQUIRED", "NO_CONFLICTS_FOUND", "NOT_EVALUATED"
    ]
    authority: tuple[AuthorityReceipt, ...]
    evidence: tuple[EvidenceSnapshot, ...]
    observations: tuple[SourceObservation, ...]
    policy_observations: tuple[PolicyObservation, ...] = ()
    plan: tuple[EvaluationUnit, ...]
    units: tuple[UnitDisposition, ...]
    findings: tuple[Finding, ...]
    proofs: tuple[ProofRecord, ...]
    coverage: EvaluationCoverage
    limitations: tuple[Limitation, ...]
    calls: tuple[CallRecord, ...]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reserved_unreported_tokens: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    pricing_revision: str | None = None
    currency: str | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.baseline.repository_id != self.revision.repository_id
            or self.baseline.github_repository_id != self.revision.github_repository_id
            or self.baseline.indexed_rkg_sha != self.revision.base_sha
        ):
            raise ValueError("Result baseline/revision mismatch")
        ids = {u.unit_id for u in self.plan}
        if (
            len(ids) != len(self.plan)
            or len(self.units) != len(ids)
            or {u.unit_id for u in self.units} != ids
        ):
            raise ValueError("Every unit needs exactly one disposition")
        ev = {e.evidence_id: e for e in self.evidence}
        proof = {p.proof_id: p for p in self.proofs}
        findings = {f.finding_id: f for f in self.findings}
        observations = {o.observation_id: o for o in self.observations}
        for o in self.policy_observations:
            e = ev.get(o.evidence_id)
            if (
                e is None
                or e.kind != "ADR"
                or (o.adr_id, o.document_hash) != (e.adr_id, e.document_hash)
                or (o.coverage == "TEXT_COMPLETE" and e.completeness != "COMPLETE")
            ):
                raise ValueError("Invalid final policy observation")
        if (
            len(ev) != len(self.evidence)
            or len(proof) != len(self.proofs)
            or len(findings) != len(self.findings)
        ):
            raise ValueError("Duplicate audit identity")
        for e in self.evidence:
            if e.repository_id != self.revision.repository_id:
                raise ValueError("Foreign result evidence")
            expected = {
                "MERGE_BASE": self.revision.merge_base_sha,
                "TARGET_BASE": self.revision.base_sha,
                "PR_HEAD": self.revision.head_sha,
            }.get(e.origin)
            if e.kind == "CODE" and expected and e.commit_sha != expected:
                raise ValueError("Result source revision mismatch")
        for p in self.proofs:
            if not set(p.observation_ids) <= observations.keys():
                raise ValueError("Dangling proof observation")
            if (
                self.engine_version in {"2", "3"}
                and p.obligation == "EXCEPTION_CLEARANCE"
                and p.supported
            ):
                if (
                    not p.clearance
                    or p.clearance.basis == "UNRESOLVED"
                    or not p.clearance.checks
                    or p.clearance.unresolved
                ):
                    raise ValueError(
                        "Supported clearance needs bounded resolved checks"
                    )
                unit = next(u for u in self.plan if u.unit_id == p.unit_id)
                if not any(
                    observations[oid].revision == "PR_HEAD"
                    and observations[oid].path == unit.path
                    and observations[oid].complete_file
                    and observations[oid].status != "UNAVAILABLE"
                    for oid in p.observation_ids
                ):
                    raise ValueError("Supported clearance needs whole head source")
                heads = [
                    observations[oid]
                    for oid in p.observation_ids
                    if observations[oid].revision == "PR_HEAD"
                    and observations[oid].path == unit.path
                ]
                if not any(
                    o.status == "ABSENT"
                    or any(
                        c.evidence_id == o.evidence_id
                        and ev[c.evidence_id].kind == "CODE"
                        and ev[c.evidence_id].origin == "PR_HEAD"
                        for c in p.citations
                    )
                    for o in heads
                ):
                    raise ValueError(
                        "Supported clearance needs cited whole-head evidence"
                    )
                if unit.channel == "ADR" and not any(
                    o.coverage == "TEXT_COMPLETE"
                    and any(c.evidence_id == o.evidence_id for c in p.citations)
                    for o in self.policy_observations
                ):
                    raise ValueError(
                        "Supported ADR clearance needs original whole policy"
                    )
        for item in (*self.proofs, *self.findings):
            if item.unit_id not in ids:
                raise ValueError("Unknown audit unit")
            for c in item.citations:
                if (
                    c.evidence_id not in ev
                    or ev[c.evidence_id].excerpt[c.start_char : c.end_char] != c.quote
                ):
                    raise ValueError("Invalid final citation")
        for f in self.findings:
            if not set(f.proof_ids) <= proof.keys():
                raise ValueError("Dangling finding proof")
            if any(proof[p].unit_id != f.unit_id for p in f.proof_ids):
                raise ValueError("Finding uses another unit's proof")
            if f.disposition == "SUPPORTED_CONFLICT":
                if self.engine_version == "3":
                    if not f.core or any(m.scope == "CORE" for m in f.missing_facts):
                        raise ValueError(
                            "Conflict needs a bounded core without blocking facts"
                        )
                    if not f.core.citations and not f.core.observation_ids:
                        raise ValueError("Conflict core needs original source support")
                    for c in f.core.citations:
                        from .evidence import resolve_quote

                        resolve_quote(c, ev)
                    if not set(f.core.observation_ids) <= observations.keys():
                        raise ValueError("Dangling core observation")
                    unit = next(u for u in self.plan if u.unit_id == f.unit_id)
                    if not any(
                        ev[c.evidence_id].kind == "CODE"
                        and ev[c.evidence_id].origin == "PR_HEAD"
                        and ev[c.evidence_id].path == unit.path
                        and ev[c.evidence_id].placement == "EXACT"
                        for c in f.core.citations
                    ) and not any(
                        observations[oid].revision == "PR_HEAD"
                        and observations[oid].path == unit.path
                        and observations[oid].complete_file
                        and observations[oid].status != "UNAVAILABLE"
                        for oid in f.core.observation_ids
                    ):
                        raise ValueError(
                            "Conflict core needs exact changed head source"
                        )
                ps = [proof[p] for p in f.proof_ids]
                required = set(OBLIGATIONS) - (
                    {"NORMATIVE_RULE"} if f.kind == "RKG_REGRESSION" else set()
                )
                if f.attribution not in {"INTRODUCED", "WORSENED"} or not f.citations:
                    raise ValueError("Conflict needs causal attribution and citations")
                if not required <= {
                    p.obligation
                    for p in ps
                    if p.supported and p.method == "SEMANTIC_CHALLENGE"
                }:
                    raise ValueError("Conflict needs reviewed obligations")
                if f.kind == "ADR_CONFLICT" and not any(
                    p.obligation == "POLICY_AUTHORITY" and p.supported for p in ps
                ):
                    raise ValueError("ADR conflict needs authority")
        for u in self.units:
            if not set(u.finding_ids) <= findings.keys() or any(
                findings[f].unit_id != u.unit_id for f in u.finding_ids
            ):
                raise ValueError("Dangling disposition finding")
        counts = (
            sum(
                u.status in {"ASSESSED", "NOT_APPLICABLE", "EXEMPT"} for u in self.units
            ),
            sum(u.status == "UNCERTAIN" for u in self.units),
            sum(u.status == "BUDGET_OMITTED" for u in self.units),
            sum(u.status == "ERROR" for u in self.units),
        )
        if counts != (
            self.coverage.assessed,
            self.coverage.uncertain,
            self.coverage.omitted,
            self.coverage.errors,
        ):
            raise ValueError("Incorrect coverage counts")
        if self.input_tokens != sum(
            c.input_tokens for c in self.calls
        ) or self.output_tokens != sum(c.output_tokens for c in self.calls):
            raise ValueError("Incorrect usage totals")
        if self.reserved_unreported_tokens != sum(
            c.unreported_reserved_tokens for c in self.calls
        ):
            raise ValueError("Incorrect unreported usage reserve")
        supported = any(f.disposition == "SUPPORTED_CONFLICT" for f in self.findings)
        if supported != (self.outcome == "CONFLICTS_FOUND"):
            raise ValueError("Outcome does not match supported findings")
        if self.outcome == "NO_CONFLICTS_FOUND" and (
            self.coverage.scope != "COMPLETE_FOR_RETAINED_SCOPE"
            or any(counts[1:])
            or self.limitations
        ):
            raise ValueError("Incomplete evaluation cannot pass")
        return self


class EvaluationInputError(ValueError):
    """Input identity cannot be trusted; no evaluation was performed."""
