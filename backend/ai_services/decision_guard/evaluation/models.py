"""Immutable wire and audit contracts for the bounded Stage 4.2 evaluator."""

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
    stable_id,
)

ID = Annotated[str, Field(min_length=1)]
Text = Annotated[str, Field(max_length=2000)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Role = Literal["MERGE_BASE", "TARGET_BASE", "PR_HEAD"]
Attribution = Literal[
    "INTRODUCED", "WORSENED", "PRE_EXISTING", "RESOLVED", "UNCHANGED", "UNKNOWN"
]


class EvaluationSettings(Contract):
    provider: Literal["azure", "google"] = "azure"
    provider_identity: str | None = None
    evaluation_profile: str = "azure-legacy-v1"
    token_encoding: Literal["o200k_base", "gemini-utf8-bound-v1"] | None = None
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
        if self.provider == "google" and (not self.provider_identity or
                self.token_encoding != "gemini-utf8-bound-v1" or self.evaluation_profile != "gemini-v1"):
            raise ValueError("Google evaluation requires persisted provider identity and Gemini sizing profile")
        if self.provider == "azure" and self.token_encoding == "gemini-utf8-bound-v1":
            raise ValueError("Gemini sizing cannot be used for Azure")
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


MAX_SPAN_CHARACTERS = 65536


class QuoteRef(Contract):
    evidence_id: ID
    quote: Annotated[str, Field(min_length=1, max_length=MAX_SPAN_CHARACTERS)]
    # Optional Python-resolved bounds preserve a precise occurrence of repeated
    # text. Legacy assessor references still require unique literal resolution.
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, gt=0)

    @property
    def display_excerpt(self) -> str:
        """Optional presentation excerpt shortened for display (default 1000 chars)."""
        return (
            self.quote
            if len(self.quote) <= 1000
            else self.quote[:1000].rstrip() + "..."
        )

    def format_excerpt(self, max_length: int = 1000) -> str:
        """Presentation excerpt with configurable length."""
        if len(self.quote) <= max_length:
            return self.quote
        return self.quote[:max_length].rstrip() + "..."

    @model_validator(mode="after")
    def span_bounds(self):
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError("Both source span bounds are required")
        if self.start_char is not None and self.end_char - self.start_char != len(
            self.quote
        ):
            raise ValueError("Source span length does not match quote")
        return self


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


class EvidenceDiscussion(Contract):
    """Evidence attached once to a bounded statement, not eight proof claims."""

    statement: Annotated[str, Field(min_length=1, max_length=1000)]
    citations: tuple[QuoteRef, ...] = Field(default=(), max_length=8)


class RevisionAssessment(Contract):
    revision: Role
    behavior: Literal["SATISFIES", "VIOLATES", "NOT_APPLICABLE", "UNKNOWN"]
    citations: tuple[QuoteRef, ...] = Field(default=(), max_length=8)
    observation_ids: tuple[ID, ...] = Field(default=(), max_length=8)


class CoreAllegation(Contract):
    boundary: Annotated[str, Field(min_length=1, max_length=400)]
    witness: Annotated[str, Field(min_length=1, max_length=600)]
    qualifiers: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        default=(), max_length=4
    )
    reachability: Annotated[str, Field(min_length=1, max_length=600)]
    citations: tuple[QuoteRef, ...] = Field(default=(), max_length=8)
    observation_ids: tuple[ID, ...] = Field(default=(), max_length=8)


class MissingFact(Contract):
    fact_id: ID
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
    claim: Annotated[str, Field(min_length=1, max_length=400)]
    condition: Annotated[str, Field(min_length=1, max_length=400)]


class ProposedIssue(Contract):
    kind: Literal["ADR_CONFLICT", "RKG_REGRESSION", "ARCHITECTURAL_IMPACT"]
    claim: Annotated[str, Field(min_length=1, max_length=600)]
    rule: EvidenceDiscussion
    core: CoreAllegation
    mechanism: Annotated[str, Field(min_length=1, max_length=600)]
    counterevidence: EvidenceDiscussion
    consequence: Annotated[str, Field(max_length=600)]
    suggested_action: Annotated[str, Field(max_length=600)]
    attribution: Attribution
    revisions: tuple[RevisionAssessment, ...] = Field(min_length=3, max_length=3)
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
        if len({f.fact_id for f in self.missing_facts}) != len(self.missing_facts):
            raise ValueError("Duplicate missing fact")
        return self


class EvidenceNeed(Contract):
    anchor_id: ID
    revision: Role
    fact_id: ID | None = None


class ResponseRejection(Contract):
    subject_id: ID
    reason: ID
    raw_json: str


class UnitProposal(Contract):
    unit_id: ID
    applicability: Literal["APPLIES", "DOES_NOT_APPLY", "EXEMPT", "UNCERTAIN"]
    normative: Literal["MANDATORY", "ADVISORY", "DESCRIPTIVE", "UNKNOWN"]
    lifecycle: Literal[
        "CURRENT_OR_UNSPECIFIED", "PROPOSED", "REJECTED", "SUPERSEDED", "UNCERTAIN"
    ]
    conclusion: Literal["ISSUES", "NO_CONFLICT", "NOT_APPLICABLE", "INSUFFICIENT"]
    explanation: Annotated[str, Field(min_length=1, max_length=1000)]
    support: tuple[QuoteRef, ...] = Field(default=(), max_length=12)
    observation_ids: tuple[ID, ...] = Field(default=(), max_length=8)
    issues: tuple[ProposedIssue, ...] = Field(default=(), max_length=4)
    needs: tuple[EvidenceNeed, ...] = Field(default=(), max_length=3)
    rejections: tuple[
        ResponseRejection, ...
    ] = ()  # Backend only; excluded from provider schema.

    @model_validator(mode="after")
    def coherent(self):
        if (self.conclusion == "ISSUES") != bool(self.issues or self.rejections):
            raise ValueError("Only ISSUES contains findings or rejected findings")
        facts = [f.fact_id for issue in self.issues for f in issue.missing_facts]
        if len(set(facts)) != len(facts):
            raise ValueError("Missing fact IDs must be unique within a unit")
        return self


class AssessmentBatchOutput(Contract):
    schema_version: Literal["dg-assessment-v2"] = "dg-assessment-v2"
    batch_id: ID
    units: tuple[UnitProposal, ...] = Field(max_length=16)


class ChallengeDecision(Contract):
    proposal_id: ID
    verdict: Literal["SUPPORTED", "REFUTED", "INSUFFICIENT"]
    explanation: Annotated[str, Field(min_length=1, max_length=1600)]
    tested_witness: Annotated[str, Field(min_length=1, max_length=600)]
    witness_correspondence: Literal["UNCHANGED", "MATERIAL_CHANGE", "UNCERTAIN"] = (
        "UNCERTAIN"
    )
    attribution: Attribution
    citations: tuple[QuoteRef, ...] = Field(default=(), max_length=12)
    observation_ids: tuple[ID, ...] = Field(default=(), max_length=8)
    revisions: tuple[RevisionAssessment, ...] = Field(min_length=3, max_length=3)
    missing_facts: tuple[MissingFact, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def coherent(self):
        if {r.revision for r in self.revisions} != {
            "MERGE_BASE",
            "TARGET_BASE",
            "PR_HEAD",
        }:
            raise ValueError("Exactly three distinct challenge revisions required")
        if len({f.fact_id for f in self.missing_facts}) != len(self.missing_facts):
            raise ValueError("Duplicate challenge missing fact")
        return self


class ChallengeBatchOutput(Contract):
    schema_version: Literal["dg-challenge-v2"] = "dg-challenge-v2"
    batch_id: ID
    decisions: tuple[ChallengeDecision, ...] = Field(max_length=16)
    rejections: tuple[ResponseRejection, ...] = ()


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

    stage: Literal["QUOTE_EXPANSION", "SPAN_RESOLUTION"]
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
    provider: Literal["azure", "google"] = "azure"
    provider_identity: str | None = None
    model_version: str | None = None
    thinking_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    wire_output_json: str | None = None
    response_diagnostics: tuple[ResponseDiagnostic, ...] = Field(
        default=(), max_length=8
    )


class PreservedAssessment(Contract):
    """Trusted backend replay input, never a substitute model-generated response.

    The caller verifies the provider artifact; evaluation revalidates the exact
    current request, response manifest and every original finding/source gate.
    """

    request: AssessmentRequest
    reply: ModelReply
    provider_response_sha256: Hash


class CallRecord(Contract):
    batch_id: ID
    phase: Literal["ASSESS", "CHALLENGE"]
    status: ID
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    unreported_reserved_tokens: int = Field(ge=0)
    denied_reservation_tokens: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(ge=0)
    request_id: str | None = None
    provider: Literal["azure", "google"] = "azure"
    provider_identity: str | None = None
    model_version: str | None = None
    thinking_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def denial_is_not_usage(self):
        if self.status in {"AUTHORIZATION_DENIED", "BUDGET_DENIED"}:
            if (
                self.input_tokens
                or self.output_tokens
                or self.unreported_reserved_tokens
                or self.request_id
            ):
                raise ValueError("Pre-dispatch denial cannot claim provider usage")
        elif self.denied_reservation_tokens:
            raise ValueError("Only denied dispatches may record denied reservations")
        return self


class ProofRecord(Contract):
    proof_id: ID
    unit_id: ID
    obligation: ID
    supported: bool
    method: Literal["SOURCE_VALIDATION", "SEMANTIC_ASSESSMENT", "SEMANTIC_CHALLENGE"]
    explanation: Text
    citations: tuple[Citation, ...] = ()
    observation_ids: tuple[ID, ...] = ()


class Finding(Contract):
    finding_id: ID
    unit_id: ID
    kind: Literal["ADR_CONFLICT", "RKG_REGRESSION", "ARCHITECTURAL_IMPACT"]
    disposition: Literal[
        "SUPPORTED_CONFLICT", "ADVISORY", "NEEDS_REVIEW", "PRE_EXISTING", "REFUTED"
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
    candidate: ProposedIssue | None = None
    review: ChallengeDecision | None = None
    review_error: str | None = None
    review_rejection: ResponseRejection | None = None

    @property
    def challenge_state(self):
        """Existing outcome fields remain authoritative; expose their review state."""
        if self.review_error in {"EVALUATION_BUDGET", "BUDGET_DENIED"}:
            return "UNREVIEWED_BUDGET"
        if self.review_error == "AUTHORIZATION_DENIED":
            return "UNREVIEWED_AUTHORIZATION"
        if self.review_error == "CHALLENGE_PACKET_TOO_LARGE":
            return "UNREVIEWED_CONTEXT"
        if self.review_error == "CHALLENGE_NOT_EXECUTED":
            return "UNREVIEWED"
        if self.review_rejection:
            return "REVIEW_INVALID"
        if self.review is not None:
            if self.review_error and not (
                self.review_error == "INSUFFICIENT_INDEPENDENT_REVIEW"
                or self.review_error.startswith("WITNESS_CORRESPONDENCE_")
            ):
                return "REVIEW_INVALID"
            return "CHALLENGED"
        if self.review_error:
            return (
                "REVIEW_INVALID"
                if self.review_error in {"INVALID", "INVALID_OUTPUT"}
                else "REVIEW_FAILED"
            )
        return "NOT_ELIGIBLE" if self.disposition == "NEEDS_REVIEW" else "NOT_REQUIRED"


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


class RejectedFinding(Contract):
    finding_id: ID
    unit_id: ID
    reason: ID
    raw_json: str


class EvaluationResult(Contract):
    """Scoped architectural result, never a repository safety certification.

    NO_CONFLICTS_FOUND means no conflict identified in successfully assessed scope.
    Consumers must retain execution, coverage, findings and errors together.
    """

    schema_version: Literal["dg-evaluation-v2"] = "dg-evaluation-v2"
    engine_version: Literal["4"] = "4"
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
    rejected_findings: tuple[RejectedFinding, ...] = ()
    validation_records_json: str = "{}"
    source_anchors: tuple[SourceAnchor, ...] = ()
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
            if f.review_rejection and (
                f.review_rejection.subject_id != f.finding_id
                or f.review_error != f.review_rejection.reason
                or f.disposition != "NEEDS_REVIEW"
            ):
                raise ValueError("Invalid independent review rejection relationship")
            if not set(f.proof_ids) <= proof.keys():
                raise ValueError("Dangling finding proof")
            if any(proof[p].unit_id != f.unit_id for p in f.proof_ids):
                raise ValueError("Finding uses another unit's proof")
            if f.candidate is None:
                raise ValueError("Finding needs its original candidate")
            if (
                f.core != f.candidate.core
                or f.claim != f.candidate.claim
                or f.kind != f.candidate.kind
            ):
                raise ValueError(
                    "Finding must preserve its exact changed head candidate and core"
                )
            unit = next(u for u in self.plan if u.unit_id == f.unit_id)
            from .validation import validate_issue, validate_review

            request = AssessmentRequest(
                batch_id="final-audit",
                context_hash=self.context_hash,
                revision=self.revision,
                units=(unit,),
                context_records_json=self.validation_records_json,
                evidence=self.evidence,
                observations=self.observations,
                authority=self.authority,
                limitations=(),
                policy_observations=self.policy_observations,
                source_anchors=self.source_anchors,
            )
            proposal = next(u.proposal for u in self.units if u.unit_id == unit.unit_id)
            if proposal is not None and f.candidate not in proposal.issues:
                raise ValueError("Finding is not part of its assessment")
            if proposal is None:
                raise ValueError("Finding needs its assessment")
            _, _, _, eligible = validate_issue(
                proposal, f.candidate, request, unit, f.finding_id
            )
            if f.disposition == "SUPPORTED_CONFLICT" and not eligible:
                raise ValueError(
                    "Conflict candidate is not source-complete or has unresolved material facts"
                )
            if f.disposition in {"SUPPORTED_CONFLICT", "REFUTED"}:
                if f.review is None or f.review_error:
                    raise ValueError(
                        "Resolved finding needs a valid independent review"
                    )
                accepted, _, attribution = validate_review(
                    f.review, request, unit, f.finding_id, f.candidate
                )
                if f.disposition == "SUPPORTED_CONFLICT" and (
                    not accepted
                    or f.review.verdict != "SUPPORTED"
                    or f.attribution != attribution
                ):
                    raise ValueError(
                        "Conflict needs independently supported introduced/worsened evidence"
                    )
                if f.disposition == "REFUTED" and (
                    not accepted or f.review.verdict != "REFUTED"
                ):
                    raise ValueError(
                        "Refuted finding needs independent counterevidence"
                    )
        if len({r.finding_id for r in self.rejected_findings}) != len(
            self.rejected_findings
        ) or any(r.unit_id not in ids for r in self.rejected_findings):
            raise ValueError("Invalid rejected finding identity")
        if any(
            u.status != "UNCERTAIN"
            for u in self.units
            if any(r.unit_id == u.unit_id for r in self.rejected_findings)
        ):
            raise ValueError("Rejected finding prevents complete unit coverage")
        for u in self.units:
            if u.proposal is not None:
                expected = {
                    stable_id(u.unit_id, str(i), issue.claim)
                    for i, issue in enumerate(u.proposal.issues)
                } | {
                    stable_id(u.unit_id, "wire-rejection", r.subject_id)
                    for r in u.proposal.rejections
                }
                recorded = set(u.finding_ids) | {
                    r.finding_id
                    for r in self.rejected_findings
                    if r.unit_id == u.unit_id
                }
                if expected != recorded:
                    raise ValueError(
                        "Every proposed or rejected finding needs an audit disposition"
                    )
            if not set(u.finding_ids) <= findings.keys() or any(
                findings[f].unit_id != u.unit_id for f in u.finding_ids
            ):
                raise ValueError("Dangling disposition finding")
            if set(u.finding_ids) != {
                f.finding_id for f in self.findings if f.unit_id == u.unit_id
            } or len(set(u.finding_ids)) != len(u.finding_ids):
                raise ValueError("Every finding needs its unit disposition link")
            if u.status != "UNCERTAIN" and any(
                f.disposition == "NEEDS_REVIEW" or f.review_error
                for f in self.findings
                if f.unit_id == u.unit_id
            ):
                raise ValueError("Unresolved finding prevents complete unit coverage")
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
        incomplete = bool(self.limitations or any(counts[1:]))
        if self.coverage.scope == "COMPLETE_FOR_RETAINED_SCOPE" and (
            incomplete or not self.units
        ):
            raise ValueError("Incomplete evaluation cannot claim complete coverage")
        if self.execution == "COMPLETED" and (counts[2] or counts[3]):
            raise ValueError("Omitted or failed units require partial execution")
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
            or self.execution != "COMPLETED"
            or not self.units
            or any(f.disposition == "NEEDS_REVIEW" for f in self.findings)
        ):
            raise ValueError("Incomplete evaluation cannot pass")
        return self


class EvaluationInputError(ValueError):
    """Input identity cannot be trusted; no evaluation was performed."""


class DispatchDenied(RuntimeError):
    """Raised ONLY when authorization rejects a request before provider dispatch.

    Network errors, lost replies and uncertain dispatches must never use this;
    they retain conservative in-flight usage reservations.
    """

    def __init__(self, reason="AUTHORIZATION_DENIED"):
        if reason not in {"AUTHORIZATION_DENIED", "BUDGET_DENIED"}:
            raise ValueError("Unknown pre-dispatch denial reason")
        super().__init__(reason)
        self.reason = reason
