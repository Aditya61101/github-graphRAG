"""Fixed, bounded LLM-first orchestration. No detectors, agents or graph writes."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass

from ..context import conservative_tokens
from ..models import Limitation, RequestUnitBudget, digest, stable_id
from .authority import authority_hash
from .evidence import SourceSupplementer, role_shas, validate_context
from .model_adapter import PROMPT_VERSION, wire_text
from .models import (
    AssessmentBatchOutput,
    AssessmentRequest,
    CallRecord,
    ChallengeBatchOutput,
    ChallengeCandidate,
    ChallengeRequest,
    DispatchDenied,
    EvaluationCoverage,
    EvaluationInputError,
    EvaluationResult,
    EvaluationSettings,
    EvaluationUnit,
    EvidenceNeed,
    Finding,
    ModelReply,
    PreservedAssessment,
    ProofRecord,
    RejectedFinding,
    SourceAnchor,
    UnitDisposition,
)
from .validation import (
    authority_established,
    source_anchors,
    source_coverage,
    validate_issue,
    validate_missing_facts,
    validate_proposal,
    validate_review,
)


def hashed(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False))


def build_units(ctx, max_units):
    groups = {}
    for d in ctx.deltas:
        groups.setdefault((d.path, d.symbols), []).append(d)
    units, overflow = [], 0
    # Deterministic requirement/subject order, without scope-metadata exclusion.
    for rule in (*ctx.requirements, None):
        for (path, symbols), deltas in sorted(groups.items()):
            if len(units) >= max_units:
                overflow += 1
                continue
            dids = {d.delta_id for d in deltas}
            candidates = [
                e
                for e in ctx.entities
                if dids.intersection(e.delta_ids) or path in e.paths
            ]
            changed = [
                e
                for e in candidates
                if e.mapping in {"CHANGED_SYMBOL", "CHANGED_CHUNK", "NEW_CANDIDATE"}
            ]
            # References in the file are not automatically core dependencies.
            # Keep changed subjects, directed neighbors and governed targets;
            # fall back conservatively when there is no mapped subject.
            governed = set(rule.target_entity_ids) if rule else set()
            entities = changed or candidates
            entities = list(
                {
                    e.entity_id: e
                    for e in (
                        *entities,
                        *(e for e in ctx.entities if e.entity_id in governed),
                    )
                }.values()
            )
            eids = {e.entity_id for e in entities}
            edges = [
                r
                for r in ctx.relationships
                if r.source_id in eids or r.target_id in eids
            ]
            endpoints = {eid for r in edges for eid in (r.source_id, r.target_id)}
            entities = [e for e in ctx.entities if e.entity_id in eids | endpoints]
            evidence = {
                eid
                for item in (*deltas, *entities, *edges, *((rule,) if rule else ()))
                for eid in item.evidence_ids
            }
            rid = rule.requirement_id if rule else "rkg-semantic-v1"
            units.append(
                EvaluationUnit(
                    unit_id=stable_id(ctx.context_hash, rid, path, *symbols),
                    channel="ADR" if rule else "RKG",
                    rule_id=rid,
                    path=path,
                    delta_ids=tuple(sorted(dids)),
                    entity_ids=tuple(sorted(e.entity_id for e in entities)),
                    relationship_ids=tuple(sorted(r.relationship_id for r in edges)),
                    evidence_ids=tuple(sorted(evidence)),
                )
            )
    # Adjacent subjects share revision sources across ADR/RKG channels. Keep
    # those together so bounded packets do not repeatedly pay for the same file.
    return tuple(
        sorted(units, key=lambda u: (u.path, u.delta_ids, u.channel, u.rule_id))
    ), overflow


def make_request(ctx, units, bundle, receipts):
    all_ev = {e.evidence_id: e for e in (*ctx.evidence, *bundle.evidence)}
    expanded = []
    for unit in units:
        paths = {
            all_ev[e].path for e in unit.evidence_ids if e in all_ev and all_ev[e].path
        } | {unit.path}
        paths.update(
            p for e in ctx.entities if e.entity_id in unit.entity_ids for p in e.paths
        )
        delta = [d for d in ctx.deltas if d.delta_id in unit.delta_ids]
        paths.update(d.previous_path for d in delta if d.previous_path)
        ids = set(unit.evidence_ids) | {
            e.evidence_id for e in bundle.evidence if e.path in paths
        }
        policy_ids = {
            aid
            for r in ctx.requirements
            if r.requirement_id == unit.rule_id
            for aid in r.adr_ids
        }
        ids.update(
            e.evidence_id
            for e in bundle.evidence
            if e.kind == "ADR" and e.adr_id in policy_ids
        )
        expanded.append(unit.model_copy(update={"evidence_ids": tuple(sorted(ids))}))
    ids = {eid for u in expanded for eid in u.evidence_ids}
    ev = tuple(all_ev[e] for e in sorted(ids))
    paths = {e.path for e in ev if e.path} | {u.path for u in units}
    records = {
        "deltas": [
            d.model_dump(mode="json")
            for d in ctx.deltas
            if any(d.delta_id in u.delta_ids for u in units)
        ],
        "entities": [
            e.model_dump(mode="json")
            for e in ctx.entities
            if any(e.entity_id in u.entity_ids for u in units)
        ],
        "relationships": [
            r.model_dump(mode="json")
            for r in ctx.relationships
            if any(r.relationship_id in u.relationship_ids for u in units)
        ],
        "requirements": [
            r.model_dump(mode="json")
            for r in ctx.requirements
            if any(r.requirement_id == u.rule_id for u in units)
        ],
    }
    anchors = {}
    for unit in expanded:
        # CODE evidence IDs already carry exact paths and unit membership. Do
        # not duplicate every revision snapshot in the auxiliary anchor table.
        pairs = [
            (e.entity_id, e.paths[0])
            for e in ctx.entities
            if e.entity_id in unit.entity_ids and len(e.paths) == 1
        ]
        pairs += [
            (d.delta_id, d.path) for d in ctx.deltas if d.delta_id in unit.delta_ids
        ]
        for anchor_id, path in pairs:
            anchors.setdefault((anchor_id, path), set()).add(unit.unit_id)
    return AssessmentRequest(
        batch_id=stable_id(
            ctx.context_hash,
            *(u.unit_id for u in units),
            hashed(bundle.model_dump(mode="json")),
            authority_hash(receipts),
        ),
        context_hash=ctx.context_hash,
        revision=ctx.revision,
        units=tuple(expanded),
        context_records_json=json.dumps(records, sort_keys=True),
        evidence=ev,
        observations=tuple(o for o in bundle.observations if o.path in paths),
        authority=receipts,
        limitations=ctx.coverage.limitations
        + bundle.limitations
        + tuple(
            Limitation(
                code="UNIT_OPTIONAL_CONTEXT_OMITTED",
                message="Unassigned same-file reference context excluded from coherent unit request",
                paths=(u.path,),
                item_ids=tuple(
                    sorted(
                        e.entity_id
                        for e in ctx.entities
                        if u.path in e.paths and e.entity_id not in u.entity_ids
                    )
                ),
            )
            for u in units
            if any(
                u.path in e.paths and e.entity_id not in u.entity_ids
                for e in ctx.entities
            )
        ),
        policy_observations=tuple(
            o for o in bundle.policy_observations if o.evidence_id in ids
        ),
        source_anchors=tuple(
            SourceAnchor(anchor_id=aid, path=path, unit_ids=tuple(sorted(ids)))
            for (aid, path), ids in sorted(anchors.items())
        ),
        adr_retrieval=ctx.coverage.adr_retrieval,
        adr_catalog_complete=ctx.coverage.adr_catalog_complete,
    )


@dataclass
class Pending:
    unit: EvaluationUnit
    proposal: object
    issue: object
    key: str
    proofs: tuple
    citations: tuple
    attribution: str
    eligible: bool


def challenge_request(ctx, items, bundle, receipts):
    """Build one canonical review packet, retaining original evidence identities."""
    unique = {p.unit.unit_id: p.unit for p in items}
    req = make_request(ctx, tuple(unique.values()), bundle, receipts)
    return ChallengeRequest(
        batch_id=stable_id(req.batch_id, "challenge", *(p.key for p in items)),
        assessment=req,
        candidates=tuple(
            ChallengeCandidate(
                proposal_id=p.key,
                unit_id=p.unit.unit_id,
                proposal=p.proposal,
                issue=p.issue,
            )
            for p in items
        ),
    )


def plan_challenges(ctx, items, bundle, receipts, budget):
    """Bounded first-fit packing; every eligible key is assigned or accounted for."""
    if len({p.key for p in items}) != len(items):
        raise EvaluationInputError("Duplicate independent review candidate")
    bins, omitted = [], {}
    for item in items:
        singleton = challenge_request(ctx, (item,), bundle, receipts)
        if not budget.fits(singleton, "CHALLENGE"):
            omitted[item.key] = "CHALLENGE_PACKET_TOO_LARGE"
            continue
        evidence = set(item.unit.evidence_ids)
        # Prefer shared original evidence; cap serializer/tokenizer probes rather
        # than performing an unbounded global bin-packing search.
        choices = sorted(
            (i for i, group in enumerate(bins) if len(group) < 16),
            key=lambda i: (
                -len(
                    evidence.intersection(
                        e for p in bins[i] for e in p.unit.evidence_ids
                    )
                ),
                i,
            ),
        )[:8]
        for index in choices:
            combined = challenge_request(ctx, (*bins[index], item), bundle, receipts)
            if budget.fits(combined, "CHALLENGE"):
                bins[index].append(item)
                break
        else:
            bins.append([item])
    packets = tuple(challenge_request(ctx, group, bundle, receipts) for group in bins)
    assigned = [c.proposal_id for packet in packets for c in packet.candidates]
    if len(assigned) != len(set(assigned)) or set(assigned) | omitted.keys() != {
        p.key for p in items
    }:
        raise EvaluationInputError("Independent review coverage mismatch")
    return packets, omitted


class RequestBudget:
    """One serialized-request estimator used by context sizing and dispatch."""

    def __init__(self, settings, counter):
        self.settings, self.counter = settings, counter

    @property
    def input_limit(self):
        return min(
            self.settings.max_input_tokens,
            self.settings.model_window - self.settings.max_output_tokens,
        )

    def count(self, request, phase):
        count = self.counter(wire_text(request, phase))
        if type(count) is not int or count < 0:
            raise EvaluationInputError("Tokenizer must return a nonnegative integer")
        return count + self.settings.provider_reserve

    def fits(self, request, phase):
        return self.count(request, phase) <= self.input_limit

    def pack(self, ctx, units, bundle, receipts):
        # Caller order is discovery bookkeeping, not an assessment dependency.
        units = sorted(
            units, key=lambda u: (u.path, u.delta_ids, u.channel, u.rule_id, u.unit_id)
        )
        originals = {u.unit_id: u for u in units}
        if len(originals) != len(units):
            raise EvaluationInputError("Duplicate assessment-unit identity")
        input_costs = {}

        def input_cost(request):
            key = tuple(u.unit_id for u in request.units)
            if key not in input_costs:
                input_costs[key] = self.count(request, "ASSESS")
            return input_costs[key]

        # Measure coherent single-unit requests before attempting any grouping.
        # An oversized unit cannot flush or displace an independently feasible one.
        feasible, omitted, measurements = [], [], []
        for unit in units:
            single = make_request(ctx, (unit,), bundle, receipts)
            cost = input_cost(single)
            measurements.append(
                RequestUnitBudget(
                    unit_id=unit.unit_id,
                    rule_id=unit.rule_id,
                    path=unit.path,
                    input_tokens=cost,
                )
            )
            (feasible if cost <= self.input_limit else omitted).append(unit)
        self.last_unit_measurements = tuple(measurements)
        batches, current = [], []
        for unit in feasible:
            if len(current) >= self.settings.max_batch_units:
                batches.append(make_request(ctx, current, bundle, receipts))
                current = []
            request = make_request(ctx, (*current, unit), bundle, receipts)
            if current and input_cost(request) > self.input_limit:
                batches.append(make_request(ctx, current, bundle, receipts))
                current = []
            current.append(unit)
        if current:
            batches.append(make_request(ctx, current, bundle, receipts))
        # Keep the existing greedy grouping, then fill earlier same-subject
        # packets. Never split/roll up a unit, mix contexts, or relax a bound.
        # Eight candidates per packet bounds exact serializer/tokenizer work;
        # this is a heuristic, not an exponential global packing search.
        packed, costs = [], []
        open_bins = defaultdict(list)
        for request in batches:
            subject_keys = {(u.path, u.delta_ids) for u in request.units}
            subject = next(iter(subject_keys)) if len(subject_keys) == 1 else None
            cost = input_cost(request)
            merged = False
            for index in tuple(open_bins[subject]) if subject is not None else ():
                previous = packed[index]
                if (
                    len(previous.units) + len(request.units)
                    > self.settings.max_batch_units
                ):
                    continue
                combined_units = sorted(
                    (*previous.units, *request.units),
                    key=lambda u: (
                        u.path,
                        u.delta_ids,
                        u.channel,
                        u.rule_id,
                        u.unit_id,
                    ),
                )
                # Reuse original units rather than their expanded packet copies:
                # make_request remains the sole authority for unit dependencies.
                combined = make_request(
                    ctx,
                    tuple(originals[u.unit_id] for u in combined_units),
                    bundle,
                    receipts,
                )
                combined_cost = input_cost(combined)
                if (
                    combined_cost <= self.input_limit
                    and combined_cost
                    <= costs[index] + cost + self.settings.max_output_tokens
                ):
                    packed[index], costs[index], merged = combined, combined_cost, True
                    if len(combined.units) >= self.settings.max_batch_units:
                        open_bins[subject].remove(index)
                    break
            if merged:
                continue
            index = len(packed)
            packed.append(request)
            costs.append(cost)
            if (
                subject is not None
                and len(request.units) < self.settings.max_batch_units
                and len(open_bins[subject]) < 8
            ):
                open_bins[subject].append(index)
        return packed, omitted


@dataclass(frozen=True)
class RequestMeasurement:
    fits: bool
    input_tokens: int
    input_limit: int
    omitted_unit_ids: tuple[str, ...] = ()
    individual_feasibility_checked: bool = False
    omitted_units: tuple[RequestUnitBudget, ...] = ()


class ContextRequestBudget(RequestBudget):
    """Deterministic Stage 4.1 sizing using the actual Stage 4.2 preparation/packets.

    Pass this callable to ArchitectureContextBuilder.request_budget; it runs in
    the existing snapshot worker. Source caching is bound to repository identity.
    No model/client is constructed. Evaluation still rechecks every dispatch.
    """

    def __init__(
        self,
        source,
        authority=None,
        *,
        settings=None,
        token_counter=conservative_tokens,
    ):
        super().__init__(settings or EvaluationSettings(), token_counter)
        self.source, self.authority = source, authority
        self.sources = {}
        self.last_packets, self.last_bundle, self.last_receipts = (), None, ()

    def __call__(self, ctx):
        from ..delta import CachedSource

        repo = ctx.revision.repository_id
        source = None
        if self.source:
            source = self.sources.setdefault(repo, CachedSource(self.source, repo))
        receipts, _, _, bundle, _ = asyncio.run(
            prepare_evaluation_evidence(ctx, self.settings, source, self.authority)
        )
        units, _ = build_units(ctx, self.settings.max_units)
        packets, omitted = self.pack(ctx, units, bundle, receipts)
        counts = [self.count(p, "ASSESS") for p in packets]
        omitted_ids = {u.unit_id for u in omitted}
        self.last_packets, self.last_bundle, self.last_receipts = (
            tuple(packets),
            bundle,
            receipts,
        )
        return RequestMeasurement(
            not omitted,
            max(counts, default=0),
            self.input_limit,
            tuple(u.unit_id for u in omitted),
            True,
            tuple(m for m in self.last_unit_measurements if m.unit_id in omitted_ids),
        )


class _Runner(RequestBudget):
    def __init__(self, model, settings, counter, clock, start):
        self.model, self.settings, self.counter, self.clock = (
            model,
            settings,
            counter,
            clock,
        )
        self.end = start + settings.deadline_seconds
        self.attempts = self.charged = self.reserved = 0
        self.cost = self.reserved_cost = 0.0
        self.records = []
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.limitations = []

    def price(self, input_tokens, output_tokens):
        s = self.settings
        return (
            input_tokens * (s.input_rate_per_million or 0)
            + output_tokens * (s.output_rate_per_million or 0)
        ) / 1_000_000

    async def call(self, request, phase):
        schema = AssessmentBatchOutput if phase == "ASSESS" else ChallengeBatchOutput
        expected = (
            {u.unit_id for u in request.units}
            if phase == "ASSESS"
            else {c.proposal_id for c in request.candidates}
        )
        for retry in range(self.settings.retries + 1):
            async with self.semaphore:
                input_reserve = self.count(request, phase)
                output_reserve = self.settings.max_output_tokens
                reserve = input_reserve + output_reserve
                price = self.price(input_reserve, output_reserve)
                if not self.fits(request, phase):
                    return None, "PACKET_TOO_LARGE"
                if (
                    self.attempts >= self.settings.max_attempts
                    or self.charged + self.reserved + reserve > self.settings.max_tokens
                    or self.clock() >= self.end
                    or (
                        self.settings.max_cost is not None
                        and self.cost + self.reserved_cost + price
                        > self.settings.max_cost
                    )
                ):
                    return None, "EVALUATION_BUDGET"
                self.attempts += 1
                self.reserved += reserve
                self.reserved_cost += price
                index = len(self.records)
                self.records.append(None)
                start = self.clock()
                reply, output, status = None, None, "MODEL_ERROR"
                denied = False
                try:
                    timeout = min(self.settings.call_timeout_seconds, self.end - start)
                    method = (
                        self.model.assess if phase == "ASSESS" else self.model.challenge
                    )
                    reply = await asyncio.wait_for(
                        method(
                            request, max_output_tokens=output_reserve, timeout=timeout
                        ),
                        timeout,
                    )
                    reply = ModelReply.model_validate_json(reply.model_dump_json())
                    status = reply.status
                    if status == "OK":
                        output = schema.model_validate_json(reply.output_json or "")
                        items = output.units if phase == "ASSESS" else output.decisions
                        ids = [
                            v.unit_id if phase == "ASSESS" else v.proposal_id
                            for v in items
                        ]
                        if phase == "CHALLENGE":
                            ids.extend(r.subject_id for r in output.rejections)
                        if (
                            output.batch_id != request.batch_id
                            or set(ids) != expected
                            or len(ids) != len(expected)
                        ):
                            raise ValueError("Response manifest mismatch")
                except DispatchDenied as error:
                    status, denied, output = error.reason, True, None
                except asyncio.CancelledError:
                    status = "CANCELLED"
                    raise
                except TimeoutError:
                    status = "MODEL_TIMEOUT"
                except Exception:  # noqa: BLE001 -- isolate provider/structured-output failures
                    status = "INVALID_OUTPUT" if reply is not None else "MODEL_ERROR"
                    output = None
                finally:
                    known_in = (
                        reply.input_tokens
                        if reply and reply.input_tokens is not None
                        else 0
                    )
                    known_out = (
                        reply.output_tokens
                        if reply and reply.output_tokens is not None
                        else 0
                    )
                    unknown_in = (
                        input_reserve
                        if reply is None or reply.input_tokens is None
                        else 0
                    )
                    unknown_out = (
                        output_reserve
                        if reply is None or reply.output_tokens is None
                        else 0
                    )
                    if denied:
                        known_in = known_out = unknown_in = unknown_out = 0
                    charged = known_in + known_out + unknown_in + unknown_out
                    self.reserved -= reserve
                    self.reserved_cost -= price
                    self.charged += charged
                    self.cost += self.price(
                        known_in + unknown_in, known_out + unknown_out
                    )
                    if charged > reserve:
                        self.limitations.append(
                            Limitation(
                                code="USAGE_RESERVATION_EXCEEDED",
                                message="Provider usage exceeded token estimate",
                            )
                        )
                        self.charged = max(self.charged, self.settings.max_tokens)
                    self.records[index] = CallRecord(
                        batch_id=request.batch_id,
                        phase=phase,
                        status=status,
                        input_tokens=known_in,
                        output_tokens=known_out,
                        unreported_reserved_tokens=unknown_in + unknown_out,
                        denied_reservation_tokens=reserve if denied else 0,
                        elapsed_ms=max(0, int((self.clock() - start) * 1000)),
                        request_id=reply.request_id if reply else None,
                    )
                if status == "OK" and output is not None:
                    return output, "OK"
                if status in {"REFUSAL", "AUTHORIZATION_DENIED", "BUDGET_DENIED"}:
                    break
            if retry < self.settings.retries:
                await asyncio.sleep(min(0.05, max(0, self.end - self.clock())))
        return None, status


async def prepare_evaluation_evidence(
    ctx,
    settings,
    source=None,
    authority=None,
    *,
    clock=time.monotonic,
    deadline=float("inf"),
):
    """Shared bounded preparation for actual evaluation and deterministic request sizing."""
    limitations = []
    receipts = ()
    if authority and ctx.adr_snapshot:
        try:
            receipts = await asyncio.to_thread(authority.receipts, ctx)
            from .models import AuthorityReceipt

            receipts = tuple(
                AuthorityReceipt.model_validate_json(r.model_dump_json())
                for r in receipts
            )
        except Exception:  # noqa: BLE001 -- an unavailable authority adapter cannot approve policy
            limitations.append(
                Limitation(
                    code="AUTHORITY_UNAVAILABLE",
                    message="Repository ADR approval could not be verified",
                )
            )
    catalog = {a.adr_id: a.content_hash for a in ctx.adr_snapshot}
    if len({r.adr_id for r in receipts}) != len(receipts) or any(
        r.repository_id != ctx.revision.repository_id
        or catalog.get(r.adr_id) != r.document_hash
        for r in receipts
    ):
        raise EvaluationInputError("Authority escaped context document identity")
    from .models import EvidenceBundle

    policy = EvidenceBundle()
    if authority and hasattr(authority, "policy_evidence") and clock() < deadline:
        task = asyncio.create_task(
            asyncio.to_thread(
                authority.policy_evidence,
                ctx,
                adr_ids={aid for r in ctx.requirements for aid in r.adr_ids},
                settings=settings,
            )
        )
        try:
            policy = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        except Exception:  # noqa: BLE001 -- optional source adapter failure must remain explicit uncertainty
            policy = EvidenceBundle(
                limitations=(
                    Limitation(
                        code="POLICY_SOURCE_UNAVAILABLE",
                        message="Original policy reader failed",
                    ),
                )
            )
        policy = EvidenceBundle.model_validate_json(policy.model_dump_json())
        catalog = {a.adr_id: a.content_hash for a in ctx.adr_snapshot}
        if any(
            e.kind != "ADR"
            or e.origin != "ADR_DOCUMENT"
            or e.repository_id != ctx.revision.repository_id
            or catalog.get(e.adr_id) != e.document_hash
            or e.content_hash != e.excerpt_hash
            for e in policy.evidence
        ):
            raise EvaluationInputError("Policy source escaped document identity")
        ev = {e.evidence_id: e for e in policy.evidence}
        if any(
            o.evidence_id not in ev
            or (o.adr_id, o.document_hash)
            != (ev[o.evidence_id].adr_id, ev[o.evidence_id].document_hash)
            or (
                o.coverage == "TEXT_COMPLETE"
                and ev[o.evidence_id].completeness != "COMPLETE"
            )
            for o in policy.policy_observations
        ):
            raise EvaluationInputError("Policy observation identity mismatch")

    def include_policy(source_bundle):
        return EvidenceBundle(
            evidence=source_bundle.evidence + policy.evidence,
            observations=source_bundle.observations,
            policy_observations=policy.policy_observations,
            limitations=source_bundle.limitations + policy.limitations,
        )

    supplement = SourceSupplementer(source, settings) if source else None

    if supplement:
        paths = {d.path for d in ctx.deltas} | {
            d.previous_path for d in ctx.deltas if d.previous_path
        }
        paths |= {e.path for e in ctx.evidence if e.kind == "CODE" and e.path}
        for path in sorted(paths):
            for role in role_shas(ctx):
                if clock() >= deadline:
                    limitations.append(
                        Limitation(
                            code="PREPARATION_DEADLINE",
                            message="Source preparation reached deadline",
                        )
                    )
                    break
                await supplement.read(ctx, path, role)
    bundle = include_policy(supplement.bundle() if supplement else EvidenceBundle())
    return receipts, supplement, include_policy, bundle, limitations


class DecisionIntegrityEvaluator:
    def __init__(
        self,
        model,
        *,
        settings=None,
        source=None,
        authority=None,
        token_counter=conservative_tokens,
        clock=time.monotonic,
    ):
        self.model = model
        self.settings = settings or EvaluationSettings()
        self.source, self.authority = source, authority
        self.counter, self.clock = token_counter, clock

    async def evaluate(
        self, context, *, preserved_assessment: PreservedAssessment | None = None
    ):
        start = self.clock()
        ctx = validate_context(context)
        settings = self.settings
        runner = _Runner(self.model, settings, self.counter, self.clock, start)
        limitations = list(ctx.coverage.limitations)
        units, overflow = build_units(ctx, settings.max_units)
        (
            receipts,
            supplement,
            include_policy,
            bundle,
            preparation_limits,
        ) = await prepare_evaluation_evidence(
            ctx,
            settings,
            self.source,
            self.authority,
            clock=self.clock,
            deadline=runner.end,
        )
        limitations.extend(preparation_limits)
        if overflow:
            limitations.append(
                Limitation(
                    code="UNIT_LEDGER_LIMIT",
                    message=f"{overflow} additional known units omitted",
                )
            )
        dispositions, proposals, pending, proof_records, findings = {}, {}, [], [], []
        rejected_findings, unit_errors = [], defaultdict(list)

        def pack(selected):
            batches, omitted = runner.pack(ctx, selected, bundle, receipts)
            for unit in omitted:
                dispositions[unit.unit_id] = UnitDisposition(
                    unit_id=unit.unit_id,
                    status="BUDGET_OMITTED",
                    reason_codes=("PACKET_TOO_LARGE",),
                )
            return batches

        async def assess(selected):
            batches = pack(selected)
            results = await asyncio.gather(
                *(runner.call(req, "ASSESS") for req in batches)
            )
            for request, (output, reason) in zip(batches, results, strict=True):
                if output is None:
                    for unit in request.units:
                        dispositions[unit.unit_id] = UnitDisposition(
                            unit_id=unit.unit_id,
                            status="BUDGET_OMITTED"
                            if reason
                            in {
                                "EVALUATION_BUDGET",
                                "PACKET_TOO_LARGE",
                                "AUTHORIZATION_DENIED",
                                "BUDGET_DENIED",
                            }
                            else "ERROR",
                            reason_codes=(reason,),
                        )
                else:
                    for proposal in output.units:
                        proposals[proposal.unit_id] = (proposal, request)

        reuse_record = None
        if preserved_assessment is None:
            await assess(units)
        else:
            preserved = PreservedAssessment.model_validate_json(
                preserved_assessment.model_dump_json()
            )
            current = make_request(ctx, units, bundle, receipts)
            if current != preserved.request:
                raise EvaluationInputError("Preserved assessment request mismatch")
            if preserved.reply.status != "OK" or not preserved.reply.request_id:
                raise EvaluationInputError(
                    "Preserved assessment requires a successful provider response"
                )
            output = AssessmentBatchOutput.model_validate_json(
                preserved.reply.output_json or ""
            )
            ids = [p.unit_id for p in output.units]
            if (
                output.batch_id != current.batch_id
                or len(ids) != len(set(ids))
                or set(ids) != {u.unit_id for u in units}
            ):
                raise EvaluationInputError("Preserved assessment manifest mismatch")
            proposals.update({p.unit_id: (p, current) for p in output.units})
            reuse_record = {
                "request_sha256": digest(preserved.request.model_dump_json()),
                "provider_response_sha256": preserved.provider_response_sha256,
                "provider_request_id": preserved.reply.request_id,
                "input_tokens": preserved.reply.input_tokens,
                "output_tokens": preserved.reply.output_tokens,
                "usage_scope": "HISTORICAL_NOT_CHARGED_AGAIN",
            }
        # Exactly one optional round; requests use only previously grounded paths.
        reassess = []
        if supplement and preserved_assessment is None:
            for unit in units:
                if unit.unit_id not in proposals:
                    continue
                proposal, request = proposals[unit.unit_id]
                old_count = len(supplement.observations)
                selected = next(u for u in request.units if u.unit_id == unit.unit_id)
                try:
                    validate_proposal(proposal, request, selected)
                except ValueError:
                    continue  # Invalid unit-level needs never dispatch source reads.
                facts = {}
                for issue in proposal.issues:
                    try:
                        validate_missing_facts(issue.missing_facts, request, selected)
                    except ValueError:
                        continue  # One bad finding must not suppress another's facts.
                    facts.update({f.fact_id: f for f in issue.missing_facts})
                needs = [
                    n
                    for n in proposal.needs
                    if n.fact_id is None
                    or (n.fact_id in facts and facts[n.fact_id].scope == "CORE")
                ]
                # A structured core fact is itself a bounded request; do not rely
                # on the model also duplicating it in proposal.needs.
                needs += [
                    EvidenceNeed(
                        anchor_id=f.anchor_id, revision=f.revision, fact_id=f.fact_id
                    )
                    for f in facts.values()
                    if f.scope == "CORE" and f.anchor_id
                ]
                for fact in facts.values():
                    if fact.scope == "CORE" and not fact.anchor_id:
                        limitations.append(
                            Limitation(
                                code="CORE_FACT_UNREQUESTABLE",
                                message=fact.unrequestable_reason,
                                item_ids=(unit.unit_id, fact.fact_id),
                            )
                        )
                anchors = source_anchors(request, selected)
                for need in dict.fromkeys(needs):
                    path = anchors.get(need.anchor_id)
                    covered = path and any(
                        o.path == path
                        and o.revision == need.revision
                        and o.complete_file
                        and o.status in {"PRESENT", "ABSENT"}
                        for o in request.observations
                    )
                    if covered:
                        limitations.append(
                            Limitation(
                                code="EVIDENCE_NEED_ALREADY_COVERED",
                                message="Requested path/revision already has complete source coverage; unresolved proof is retained",
                                paths=(path,),
                                item_ids=(unit.unit_id, need.anchor_id)
                                + ((need.fact_id,) if need.fact_id else ()),
                            )
                        )
                        continue
                    if path and self.clock() < runner.end:
                        await supplement.read(ctx, path, need.revision)
                if len(supplement.observations) > old_count:
                    reassess.append(unit)
            bundle = include_policy(supplement.bundle())
            if reassess:
                # Remove superseded proposals: a failed reassessment cannot reuse a stale clean verdict.
                for unit in reassess:
                    proposals.pop(unit.unit_id, None)
                await assess(reassess)

        for unit in units:
            if unit.unit_id not in proposals:
                continue
            proposal, request = proposals[unit.unit_id]
            selected = next(u for u in request.units if u.unit_id == unit.unit_id)
            for rejection in proposal.rejections:
                key = stable_id(unit.unit_id, "wire-rejection", rejection.subject_id)
                rejected_findings.append(
                    RejectedFinding(
                        finding_id=key,
                        unit_id=unit.unit_id,
                        reason=rejection.reason,
                        raw_json=rejection.raw_json,
                    )
                )
                unit_errors[unit.unit_id].append(rejection.reason)
            try:
                validate_proposal(proposal, request, selected)
            except ValueError as error:
                unit_errors[unit.unit_id].append(str(error))
            if not proposal.issues:
                inactive = proposal.lifecycle in {"PROPOSED", "REJECTED", "SUPERSEDED"}
                uncertain = bool(
                    unit_errors[unit.unit_id]
                    or proposal.rejections
                    or proposal.conclusion == "INSUFFICIENT"
                    or proposal.applicability == "UNCERTAIN"
                    or proposal.needs
                )
                uncertain |= unit.channel == "ADR" and (
                    proposal.normative == "UNKNOWN"
                    or proposal.lifecycle == "UNCERTAIN"
                    or (not inactive and not authority_established(request, selected))
                )
                status = (
                    "UNCERTAIN"
                    if uncertain
                    else "EXEMPT"
                    if proposal.applicability == "EXEMPT"
                    else "NOT_APPLICABLE"
                    if proposal.conclusion == "NOT_APPLICABLE" or inactive
                    else "ASSESSED"
                )
                dispositions[unit.unit_id] = UnitDisposition(
                    unit_id=unit.unit_id,
                    status=status,
                    reason_codes=tuple(unit_errors[unit.unit_id])
                    or (("UNRESOLVED_ASSESSMENT",) if uncertain else ()),
                    proposal=proposal,
                )
                continue
            for i, issue in enumerate(proposal.issues):
                key = stable_id(unit.unit_id, str(i), issue.claim)
                try:
                    ps, cs, attribution, eligible = validate_issue(
                        proposal, issue, request, selected, key
                    )
                    pending.append(
                        Pending(
                            selected,
                            proposal,
                            issue,
                            key,
                            ps,
                            cs,
                            attribution,
                            eligible,
                        )
                    )
                except ValueError as error:
                    rejected_findings.append(
                        RejectedFinding(
                            finding_id=key,
                            unit_id=unit.unit_id,
                            reason=str(error),
                            raw_json=issue.model_dump_json(),
                        )
                    )
                    unit_errors[unit.unit_id].append(str(error))

        decisions, review_rejections = {}, {}
        eligible = [p for p in pending if p.eligible]
        challenge_batches, review_errors = plan_challenges(
            ctx, eligible, bundle, receipts, runner
        )
        results = await asyncio.gather(
            *(runner.call(req, "CHALLENGE") for req in challenge_batches)
        )
        for req, (output, reason) in zip(challenge_batches, results, strict=True):
            if output:
                for decision in output.decisions:
                    decisions[decision.proposal_id] = (decision, req.assessment)
                for rejection in output.rejections:
                    review_errors[rejection.subject_id] = rejection.reason
                    review_rejections[rejection.subject_id] = rejection
            else:
                for candidate in req.candidates:
                    review_errors[candidate.proposal_id] = reason

        for item in pending:
            ps, citations = list(item.proofs), list(item.citations)
            disposition = "NEEDS_REVIEW"
            if (
                item.issue.kind == "ARCHITECTURAL_IMPACT"
                or item.proposal.normative in {"ADVISORY", "DESCRIPTIVE"}
                and item.unit.channel == "ADR"
            ):
                disposition = "ADVISORY"
            elif (
                item.attribution == "PRE_EXISTING"
                and item.proposal.applicability == "APPLIES"
            ):
                disposition = "PRE_EXISTING"
            elif (
                item.attribution in {"RESOLVED", "UNCHANGED"}
                and item.proposal.applicability == "APPLIES"
            ):
                disposition = "ADVISORY"
            request = proposals[item.unit.unit_id][1]
            if (
                any(f.scope == "CORE" for f in item.issue.missing_facts)
                or item.proposal.applicability == "UNCERTAIN"
                or any(n.fact_id is None for n in item.proposal.needs)
                or not source_coverage(request, item.unit)
                or (
                    item.unit.channel == "ADR"
                    and (
                        item.proposal.normative == "UNKNOWN"
                        or item.proposal.lifecycle == "UNCERTAIN"
                    )
                )
            ):
                disposition = "NEEDS_REVIEW"
            missing_facts = item.issue.missing_facts
            decision = None
            review_error = review_errors.get(item.key)
            if item.eligible:
                review_error = review_error or (
                    "CHALLENGE_NOT_EXECUTED" if item.key not in decisions else None
                )
            if item.eligible and item.key in decisions:
                decision, req = decisions[item.key]
                selected = next(u for u in req.units if u.unit_id == item.unit.unit_id)
                try:
                    accepted, checked_citations, attribution = validate_review(
                        decision, req, selected, item.key, item.issue
                    )
                    citations.extend(checked_citations)
                    missing_facts += decision.missing_facts
                    ps.append(
                        ProofRecord(
                            proof_id=stable_id(item.key, "independent-review"),
                            unit_id=item.unit.unit_id,
                            obligation="INDEPENDENT_REVIEW",
                            supported=accepted and decision.verdict == "SUPPORTED",
                            method="SEMANTIC_CHALLENGE",
                            explanation=decision.explanation,
                            citations=checked_citations,
                            observation_ids=decision.observation_ids,
                        )
                    )
                    if accepted and decision.verdict == "SUPPORTED":
                        disposition = "SUPPORTED_CONFLICT"
                        item.attribution = attribution
                    elif accepted and decision.verdict == "REFUTED":
                        disposition = "REFUTED"
                    else:
                        review_error = (
                            "WITNESS_CORRESPONDENCE_" + decision.witness_correspondence
                            if decision.witness_correspondence != "UNCHANGED"
                            else "INSUFFICIENT_INDEPENDENT_REVIEW"
                        )
                except ValueError as error:
                    review_error = str(error)
            proof_records.extend(ps)
            unique_cites = tuple(dict.fromkeys(citations))
            ev = {e.evidence_id: e for e in (*ctx.evidence, *bundle.evidence)}
            loc = next(
                (
                    c.location
                    for c in unique_cites
                    if ev[c.evidence_id].origin == "PR_HEAD" and c.location
                ),
                None,
            )
            findings.append(
                Finding(
                    finding_id=item.key,
                    unit_id=item.unit.unit_id,
                    kind=item.issue.kind,
                    disposition=disposition,
                    attribution=item.attribution,
                    claim=item.issue.claim,
                    consequence=item.issue.consequence,
                    suggested_action=item.issue.suggested_action,
                    core=item.issue.core,
                    missing_facts=missing_facts,
                    potential_consequences=item.issue.potential_consequences,
                    citations=unique_cites,
                    proof_ids=tuple(p.proof_id for p in ps),
                    head_location=loc,
                    candidate=item.issue,
                    review=decision,
                    review_error=review_error,
                    review_rejection=review_rejections.get(item.key),
                )
            )
        # Resolve coverage once, after every sibling finding has been processed.
        for unit in units:
            if unit.unit_id not in proposals:
                continue
            own = [f for f in findings if f.unit_id == unit.unit_id]
            if not own and not unit_errors[unit.unit_id]:
                continue
            uncertain = bool(unit_errors[unit.unit_id]) or any(
                f.disposition == "NEEDS_REVIEW" for f in own
            )
            dispositions[unit.unit_id] = UnitDisposition(
                unit_id=unit.unit_id,
                status="UNCERTAIN" if uncertain else "ASSESSED",
                reason_codes=tuple(
                    dict.fromkeys(
                        unit_errors[unit.unit_id]
                        + [f.review_error for f in own if f.review_error]
                        + [
                            f.challenge_state
                            for f in own
                            if f.disposition == "NEEDS_REVIEW"
                        ]
                    )
                ),
                finding_ids=tuple(f.finding_id for f in own),
                proposal=proposals[unit.unit_id][0],
            )

        limitations.extend(bundle.limitations)
        limitations.extend(runner.limitations)
        # Complete file accounting survives missing deltas/candidate pruning upstream.
        for f in ctx.coverage.files:
            if f.status != "TEXT" or f.reasons:
                limitations.append(
                    Limitation(
                        code="FILE_COVERAGE",
                        message=";".join(f.reasons) or f.status,
                        paths=(f.path,),
                    )
                )
        if (
            ctx.coverage.adr_retrieval == "DEGRADED"
            or not ctx.coverage.adr_catalog_complete
        ):
            limitations.append(
                Limitation(code="ADR_COVERAGE", message="ADR discovery incomplete")
            )
        final_units = tuple(
            dispositions.get(
                u.unit_id,
                UnitDisposition(
                    unit_id=u.unit_id,
                    status="ERROR",
                    reason_codes=("UNACCOUNTED_UNIT",),
                ),
            )
            for u in units
        )
        counts = (
            sum(
                u.status in {"ASSESSED", "NOT_APPLICABLE", "EXEMPT"}
                for u in final_units
            ),
            sum(u.status == "UNCERTAIN" for u in final_units),
            sum(u.status == "BUDGET_OMITTED" for u in final_units),
            sum(u.status == "ERROR" for u in final_units),
        )
        partial = bool(limitations or any(counts[1:]))
        supported = any(f.disposition == "SUPPORTED_CONFLICT" for f in findings)
        outcome = (
            "CONFLICTS_FOUND"
            if supported
            else "NOT_EVALUATED"
            if not proposals
            else "REVIEW_REQUIRED"
            if partial
            else "NO_CONFLICTS_FOUND"
        )
        evidence = tuple(
            sorted(
                {e.evidence_id: e for e in (*ctx.evidence, *bundle.evidence)}.values(),
                key=lambda e: e.evidence_id,
            )
        )
        b_hash, s_hash, a_hash = (
            hashed(bundle.model_dump(mode="json")),
            hashed(settings.model_dump(mode="json")),
            authority_hash(receipts),
        )
        calls = tuple(runner.records)
        audit_request = make_request(ctx, units, bundle, receipts)
        audit_records = json.loads(audit_request.context_records_json)
        if reuse_record:
            audit_records["preserved_assessment"] = reuse_record
        return EvaluationResult(
            evaluation_id=stable_id(
                ctx.context_hash,
                b_hash,
                s_hash,
                a_hash,
                PROMPT_VERSION,
                self.model.deployment_revision,
            ),
            context_hash=ctx.context_hash,
            bundle_hash=b_hash,
            settings_hash=s_hash,
            authority_hash=a_hash,
            revision=ctx.revision,
            baseline=ctx.baseline,
            deployment_revision=self.model.deployment_revision,
            tokenizer=getattr(
                self.counter, "name", getattr(self.counter, "__name__", "injected")
            ),
            execution="PARTIAL" if counts[2] or counts[3] or overflow else "COMPLETED",
            outcome=outcome,
            authority=receipts,
            evidence=evidence,
            observations=bundle.observations,
            policy_observations=bundle.policy_observations,
            plan=audit_request.units,
            validation_records_json=json.dumps(audit_records)
            if reuse_record
            else audit_request.context_records_json,
            source_anchors=audit_request.source_anchors,
            rejected_findings=tuple(rejected_findings),
            units=final_units,
            findings=tuple(sorted(findings, key=lambda f: f.finding_id)),
            proofs=tuple(proof_records),
            coverage=EvaluationCoverage(
                assessed=counts[0],
                uncertain=counts[1],
                omitted=counts[2],
                errors=counts[3],
                scope="UNASSESSABLE"
                if not proposals
                else "PARTIAL"
                if partial
                else "COMPLETE_FOR_RETAINED_SCOPE",
            ),
            limitations=tuple(dict.fromkeys(limitations)),
            calls=calls,
            input_tokens=sum(c.input_tokens for c in calls),
            output_tokens=sum(c.output_tokens for c in calls),
            reserved_unreported_tokens=sum(c.unreported_reserved_tokens for c in calls),
            elapsed_ms=max(0, int((self.clock() - start) * 1000)),
            estimated_cost=runner.cost
            if settings.input_rate_per_million is not None
            else None,
            pricing_revision=settings.pricing_revision,
            currency=settings.currency,
        )
