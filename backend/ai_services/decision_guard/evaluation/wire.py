"""Role-bound provider references; expand only explicitly selected original sources.

One batch-level table avoids repeating long evidence hashes and quotations.
The public assessment/proof/result contracts retain fully resolved references.
"""

import json
from types import SimpleNamespace
from typing import Annotated, ClassVar, Literal

from pydantic import Field, model_validator

from ..budget import coverage_summary
from ..models import Contract, EvidenceSnapshot, digest
from .models import (
    AssessmentBatchOutput,
    ChallengeBatchOutput,
    ChallengeDecision,
    CoreAllegation,
    EvidenceDiscussion,
    ProposedIssue,
    ResponseRejection,
    RevisionAssessment,
    UnitProposal,
)

ROLE_PREFIX = {
    "MERGE_BASE": "M",
    "TARGET_BASE": "T",
    "PR_HEAD": "H",
    "ADR_DOCUMENT": "A",
    "GRAPH_ASSERTION": "G",
}
QuoteID = Annotated[str, Field(pattern=r"^[MTHAG][1-9][0-9]{0,3}$")]
SourceRef = Annotated[
    str,
    Field(
        pattern=r"^(MERGE_BASE|TARGET_BASE|PR_HEAD|ADR_DOCUMENT|GRAPH_ASSERTION):[1-9][0-9]*$"
    ),
]
Brief = Annotated[str, Field(max_length=600)]


class NamedQuote(Contract):
    quote_id: QuoteID
    source_ref: SourceRef
    quote: Annotated[str, Field(min_length=1, max_length=1000)]


def source_references(request):
    """Request-local, explicit ownership; never infer a revision from shared bytes."""
    assessment = request.assessment if hasattr(request, "assessment") else request
    if not assessment.evidence:
        return {}
    roles = {
        "MERGE_BASE": assessment.revision.merge_base_sha,
        "TARGET_BASE": assessment.revision.base_sha,
        "PR_HEAD": assessment.revision.head_sha,
    }
    index, seen, counts = {}, set(), {}
    for e in sorted(
        assessment.evidence, key=lambda e: (e.origin, e.path or "", e.evidence_id)
    ):
        if e.evidence_id in seen:
            raise ValueError("AMBIGUOUS_SOURCE_IDENTITY")
        seen.add(e.evidence_id)
        if (
            e.repository_id != assessment.revision.repository_id
            or digest(e.excerpt) != e.excerpt_hash
        ):
            raise ValueError("INVALID_EVIDENCE_IDENTITY")
        if e.kind == "CODE":
            if e.origin not in roles:
                continue  # Indexed discovery evidence stays visible, but is not selectable code.
            if e.commit_sha != roles[e.origin]:
                raise ValueError("WRONG_SOURCE_REVISION")
            role = e.origin
        elif e.kind == "ADR" and e.origin == "ADR_DOCUMENT":
            role = "ADR_DOCUMENT"
        elif e.kind == "GRAPH_ASSERTION":
            role = "GRAPH_ASSERTION"
        else:
            continue
        counts[role] = counts.get(role, 0) + 1
        index[f"{role}:{counts[role]}"] = e
    return index


class WireDiscussion(EvidenceDiscussion):
    citations: tuple[QuoteID, ...] = Field(default=(), max_length=8)


class WireRevision(RevisionAssessment):
    citations: tuple[QuoteID, ...] = Field(default=(), max_length=8)


class WireCore(CoreAllegation):
    citations: tuple[QuoteID, ...] = Field(default=(), max_length=8)


class WireIssue(ProposedIssue):
    rule: WireDiscussion
    core: WireCore
    counterevidence: WireDiscussion
    revisions: tuple[WireRevision, ...] = Field(min_length=3, max_length=3)


class WireUnit(UnitProposal):
    support: tuple[QuoteID, ...] = Field(default=(), max_length=12)
    issues: tuple[WireIssue, ...] = Field(default=(), max_length=4)
    rejections: ClassVar[tuple] = ()


class WireDecision(ChallengeDecision):
    citations: tuple[QuoteID, ...] = Field(default=(), max_length=12)
    revisions: tuple[WireRevision, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def coherent(self):
        # Cross-field checks run per finding after span resolution. A duplicate
        # revision in one decision must not erase its independently valid sibling.
        return self


class NamedSpan(Contract):
    span_id: QuoteID
    source_ref: SourceRef
    # Bounds are checked per referenced span, preserving finding-level isolation.
    start_line: int
    end_line: int
    start_column: int = 0
    end_column: int | None = None


def expand(payload, schema, request, *, allow_unused=True, diagnostics=None):
    data = payload.model_dump(mode="json")
    data["schema_version"] = (
        "dg-assessment-v2" if schema is AssessmentBatchOutput else "dg-challenge-v2"
    )
    definitions = data.pop("quotes")
    sources = source_references(request)
    pool, pairs, used = {}, set(), set()
    for item in definitions:
        key = item.pop("quote_id")
        pair = item["source_ref"], item["quote"]
        if key in pool or pair in pairs:
            raise ValueError("DUPLICATE_QUOTE_DEFINITION")
        pool[key] = item
        pairs.add(pair)

    def walk(value, role=None):
        if isinstance(value, dict):
            if "revision" in value and "citations" in value:
                role = value["revision"]
            for key, entry in value.items():
                if key in {"citations", "support"}:
                    if not set(entry) <= pool.keys():
                        raise ValueError("UNKNOWN_QUOTE_ID")
                    used.update(entry)
                    resolved = []
                    for q in entry:
                        ref = pool[q]
                        source = sources.get(ref["source_ref"])
                        if source is None:
                            raise ValueError("UNKNOWN_SOURCE_REF")
                        selected_role = ref["source_ref"].split(":", 1)[0]
                        if q[0] != ROLE_PREFIX[selected_role]:
                            raise ValueError("QUOTE_SOURCE_ROLE_MISMATCH")
                        if role and (source.kind != "CODE" or source.origin != role):
                            raise ValueError("WRONG_REVISION_CITATION")
                        resolved.append(
                            {"evidence_id": source.evidence_id, "quote": ref["quote"]}
                        )
                    value[key] = resolved
                else:
                    walk(entry, "PR_HEAD" if key == "core" else role)
        elif isinstance(value, list):
            for entry in value:
                walk(entry, role)

    if schema is AssessmentBatchOutput:
        for unit in data["units"]:
            raw_unit = json.dumps(unit)
            issues = unit.pop("issues")
            accepted, rejected = [], []
            try:
                walk(unit)
            except ValueError as error:
                if str(error) not in {
                    "UNKNOWN_SOURCE_REF",
                    "QUOTE_SOURCE_ROLE_MISMATCH",
                }:
                    raise
                # Reject the unit's support, never rebind its selection. Preserve
                # the original wire unit and keep independently valid siblings.
                rejected.append(
                    ResponseRejection(
                        subject_id="unit-support", reason=str(error), raw_json=raw_unit
                    ).model_dump(mode="json")
                )
                unit.update(support=[], observation_ids=[], conclusion="ISSUES")
            for index, issue in enumerate(issues):
                raw = json.dumps(issue)
                try:
                    walk(issue)
                    accepted.append(issue)
                except ValueError as error:
                    rejected.append(
                        ResponseRejection(
                            subject_id=str(index), reason=str(error), raw_json=raw
                        ).model_dump(mode="json")
                    )
            unit.update(issues=accepted, rejections=rejected)
    else:
        accepted, rejected = [], []
        for decision in data["decisions"]:
            raw = json.dumps(decision)
            try:
                walk(decision)
                accepted.append(decision)
            except ValueError as error:
                rejected.append(
                    ResponseRejection(
                        subject_id=decision["proposal_id"],
                        reason=str(error),
                        raw_json=raw,
                    ).model_dump(mode="json")
                )
        data.update(decisions=accepted, rejections=rejected)
    unused = tuple(sorted(pool.keys() - used))
    if unused and not allow_unused:
        raise ValueError("UNUSED_QUOTE_DEFINITION")
    output = schema.model_validate_json(json.dumps(data))
    if unused and diagnostics is not None:
        diagnostics.append(unused)
    return output


class ProviderContract(Contract):
    @classmethod
    def model_json_schema(cls, **kwargs):
        schema = super().model_json_schema(**kwargs)

        def strip_titles(node):
            if isinstance(node, dict):
                node.pop("title", None)
                for value in node.values():
                    strip_titles(value)
            elif isinstance(node, list):
                for value in node:
                    strip_titles(value)

        strip_titles(
            schema
        )  # Cosmetic labels only; retain every type/bound/description.
        return schema


class WireAssessment(ProviderContract):
    schema_version: Literal["dg-assessment-wire-v3"] = "dg-assessment-wire-v3"
    batch_id: str
    quotes: tuple[NamedQuote, ...] = Field(max_length=128)
    units: tuple[WireUnit, ...] = Field(max_length=16)

    def expanded_with_diagnostics(self, request):
        """Ignore only unreferenced, schema-valid definitions; never grant proof."""
        diagnostics = []
        output = expand(
            self,
            AssessmentBatchOutput,
            request,
            allow_unused=True,
            diagnostics=diagnostics,
        )
        return output, diagnostics[0] if diagnostics else ()

    def expanded(self, request):
        return self.expanded_with_diagnostics(request)[0]


class WireChallenge(ProviderContract):
    schema_version: Literal["dg-challenge-spans-v1"] = "dg-challenge-spans-v1"
    batch_id: str
    spans: tuple[NamedSpan, ...] = Field(max_length=128)
    decisions: tuple[WireDecision, ...] = Field(max_length=16)

    def expanded(self, request):
        from .challenge import expand_challenge

        return expand_challenge(self, request)


def intern_refs(data, request, prefix=""):
    """Replace references on a JSON copy; keep each exact ID/text pair once."""
    pool = {}
    definitions = []
    sources = {e.evidence_id: ref for ref, e in source_references(request).items()}

    def walk(value):
        if isinstance(value, dict):
            for key, entry in value.items():
                if key in {"citations", "support"}:
                    ids = []
                    for ref in entry:
                        pair = ref["evidence_id"], ref["quote"]
                        if pair not in pool:
                            source_ref = sources.get(ref["evidence_id"])
                            if source_ref is None:
                                raise ValueError("UNSELECTABLE_EVIDENCE_REF")
                            role = source_ref.split(":", 1)[0]
                            pool[pair] = prefix + ROLE_PREFIX[role] + str(len(pool) + 1)
                            definitions.append(
                                {
                                    "quote_id": pool[pair],
                                    "source_ref": source_ref,
                                    "quote": ref["quote"],
                                }
                            )
                        ids.append(pool[pair])
                    value[key] = ids
                else:
                    walk(entry)
        elif isinstance(value, list):
            for entry in value:
                walk(entry)

    walk(data)
    return definitions


def compact(output, request):
    """Lossless offline conversion for tests/size comparison, never citation repair."""
    if isinstance(output, ChallengeBatchOutput):
        from .challenge import compact_challenge

        return compact_challenge(output, request)
    data = output.model_dump(mode="json")
    data.pop("rejections", None)
    for unit in data.get("units", ()):
        if unit.pop("rejections", ()):
            raise ValueError("Cannot compact rejected audit data as a new response")
    data["quotes"] = intern_refs(data, request)
    schema = (
        WireAssessment if isinstance(output, AssessmentBatchOutput) else WireChallenge
    )
    data["schema_version"] = (
        "dg-assessment-wire-v3" if schema is WireAssessment else "dg-challenge-wire-v3"
    )
    return schema.model_validate_json(json.dumps(data))


def intern_source_texts(assessment):
    """Lossless byte-text sharing, including indexed slices of complete sources.

    Every source identity/revision remains separate. Canonical requests retain
    original excerpts; provider refs slice the exact same text, never repaired text.
    """
    texts = []
    for snapshot in sorted(
        assessment["evidence"], key=lambda e: (-len(e["excerpt"]), e["evidence_id"])
    ):
        excerpt = snapshot.pop("excerpt")
        found = next(
            (
                (item, item["text"].find(excerpt))
                for item in texts
                if excerpt in item["text"]
            ),
            None,
        )
        if found is None:
            item = {
                "text_id": "t" + str(len(texts) + 1),
                "text": excerpt,
                "text_hash": digest(excerpt),
            }
            texts.append(item)
            start = 0
        else:
            item, start = found
        snapshot["excerpt_ref"] = {
            "text_id": item["text_id"],
            "start_char": start,
            "end_char": start + len(excerpt),
        }
        for key in [k for k, value in snapshot.items() if value is None]:
            snapshot.pop(key)  # Optional null labels carry no source facts.
    assessment["source_texts"] = texts
    assessment["input_format"] = (
        "dg-request-text-v2"
        if any("source_ref" in e or "read_only" in e for e in assessment["evidence"])
        else "dg-request-text-v1"
    )


def expand_source_texts(assessment):
    """Local verification helper: expand a JSON copy for exact identity audits."""
    data = json.loads(json.dumps(assessment))
    definitions = data.pop("source_texts")
    if len({x["text_id"] for x in definitions}) != len(definitions) or any(
        digest(x["text"]) != x["text_hash"] for x in definitions
    ):
        raise ValueError("Provider source text table integrity mismatch")
    texts = {x["text_id"]: x["text"] for x in definitions}
    version = data.pop("input_format")
    supplied_refs = {}
    for index, snapshot in enumerate(data["evidence"]):
        ref = snapshot.pop("excerpt_ref")
        if ref["text_id"] not in texts or not 0 <= ref["start_char"] <= ref[
            "end_char"
        ] <= len(texts[ref["text_id"]]):
            raise ValueError("Provider source slice outside text")
        snapshot["excerpt"] = texts[ref["text_id"]][ref["start_char"] : ref["end_char"]]
        supplied_refs[snapshot["evidence_id"]] = snapshot.pop("source_ref", None)
        snapshot.pop("read_only", None)
        if digest(snapshot["excerpt"]) != snapshot["excerpt_hash"]:
            raise ValueError("Provider source projection hash mismatch")
        data["evidence"][index] = EvidenceSnapshot.model_validate_json(
            json.dumps(snapshot)
        ).model_dump(mode="json")
    if version == "dg-request-text-v2":
        request = SimpleNamespace(
            revision=SimpleNamespace(**data["revision"]),
            evidence=tuple(
                EvidenceSnapshot.model_validate(e) for e in data["evidence"]
            ),
        )
        expected = {e.evidence_id: ref for ref, e in source_references(request).items()}
        if supplied_refs != {
            e["evidence_id"]: expected.get(e["evidence_id"]) for e in data["evidence"]
        }:
            raise ValueError("Provider source reference ownership mismatch")
    return data


def compact_context_records(assessment):
    """Remove repeated discovery bookkeeping, preserving scope/qualifier hints."""
    records = json.loads(assessment["context_records_json"])
    for entity in records["entities"]:
        entity.pop("evidence_ids", None)  # Unit manifest + source paths supply the IDs.
        entity.pop("delta_ids", None)
        entity.pop("reason", None)
    for relationship in records["relationships"]:
        relationship.pop(
            "rationale", None
        )  # Exact original assertion text is evidence.
    for rule in records["requirements"]:
        raw = rule.pop("declared_applicability_json", None)
        if raw:
            try:
                metadata = json.loads(raw)
            except (TypeError, ValueError):
                rule["scope_hints"] = {"invalid": True}
            else:
                rule["scope_hints"] = {
                    k: metadata[k]
                    for k in (
                        "applicability",
                        "entity_ids",
                        "community_ids",
                        "unresolved_targets",
                        "context_truncated",
                    )
                    if k in metadata
                }
        # Coalesce exact duplicate target identities without losing any edge
        # type, name, origin or other link attribute. These are discovery hints.
        grouped = {}
        for link in rule.get("target_links", ()):
            identity = {k: v for k, v in link.items() if k != "relationship"}
            key = json.dumps(identity, sort_keys=True)
            row = grouped.setdefault(key, {**identity, "relationships": []})
            if link["relationship"] not in row["relationships"]:
                row["relationships"].append(link["relationship"])
        if "target_links" in rule:
            rule["target_links"] = list(grouped.values())
        # Remove generated explanatory prose, never conditions, exceptions,
        # qualifications, uncertainty, original support or source references.
        applicability = rule.get("scope_hints", {}).get("applicability", {})
        if isinstance(applicability, dict):
            applicability.pop("rationale", None)
            for target in applicability.get("targets", ()):
                target.pop("description", None)
                if target.get("label") is None:
                    target.pop("label", None)
        for key in (
            "enrichment_fingerprint",
            "enrichment_status",
            "enrichment_model",
            "relevance_score",
            "discovery_reasons",
        ):
            rule.pop(key, None)
    assessment["context_records_json"] = json.dumps(
        records, ensure_ascii=False, separators=(",", ":")
    )


def provider_request_json(request, phase):
    if phase == "CHALLENGE":
        from .challenge import challenge_projection

        return json.dumps(
            challenge_projection(request), ensure_ascii=False, separators=(",", ":")
        )
    data = request.model_dump(mode="json")
    assessment = data
    limitations = request.limitations
    assessment["coverage"] = coverage_summary(
        limitations,
        assessment["revision"]["repository_id"],
        relevant_ids=[d for u in assessment["units"] for d in u["delta_ids"]],
        relevant_paths=[u["path"] for u in assessment["units"]],
    )
    assessment["coverage"].update(
        adr_retrieval=assessment.pop("adr_retrieval"),
        adr_catalog_complete=assessment.pop("adr_catalog_complete"),
    )
    assessment.pop("limitations")
    refs = {e.evidence_id: ref for ref, e in source_references(request).items()}
    for e in assessment["evidence"]:
        if e["evidence_id"] in refs:
            e["source_ref"] = refs[e["evidence_id"]]
        else:
            e["read_only"] = True
    intern_source_texts(assessment)
    compact_context_records(assessment)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
