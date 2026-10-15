"""Focused independent-review projection and exact backend-resolved source spans.

The canonical request/evidence never changes. Display coordinates refer to the
numbered shared source text, and must stay inside the selected original snapshot.
"""

import bisect
import json
from types import SimpleNamespace

from pydantic import ValidationError

from ..budget import coverage_summary
from ..models import EvidenceSnapshot, digest
from .evidence import resolve_quote
from .models import (
    MAX_SPAN_CHARACTERS,
    ChallengeBatchOutput,
    ChallengeDecision,
    QuoteRef,
    ResponseRejection,
)
from .wire import ROLE_PREFIX, intern_source_texts, source_references


def _lines(text):
    lines = text.splitlines(keepends=True)
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    return lines, starts


def _coordinates(text, start, end):
    lines, starts = _lines(text)
    if not lines or not 0 <= start < end <= len(text):
        raise ValueError("INVALID_SOURCE_SPAN")
    first = bisect.bisect_right(starts, start) - 1
    last = bisect.bisect_right(starts, end - 1) - 1
    return {
        "start_line": first + 1,
        "end_line": last + 1,
        "start_column": start - starts[first],
        "end_column": None
        if end - starts[last] == len(lines[last])
        else end - starts[last],
    }


def _catalog(request):
    selectable = source_references(request)
    evidence = request.assessment.evidence
    complete = [
        e
        for e in selectable.values()
        if e.kind == "CODE" and e.completeness == "COMPLETE" and e.placement == "EXACT"
    ]
    visible, redundant = [], 0
    aliases = {e.evidence_id: ref for ref, e in selectable.items()}
    for e in evidence:
        ref = aliases.get(e.evidence_id)
        if (
            ref is None
            and e.kind == "CODE"
            and any(
                (e.path, e.commit_sha) == (v.path, v.commit_sha)
                and e.excerpt in v.excerpt
                for v in complete
            )
        ):
            redundant += 1  # Exact bytes are still available, with verified provenance.
            continue
        row = e.model_dump(mode="json")
        row.update({"source_ref": ref} if ref else {"read_only": True})
        visible.append(row)
    catalog = {"evidence": visible}
    intern_source_texts(catalog)
    by_id = {r["evidence_id"]: r for r in catalog["evidence"]}
    texts = {t["text_id"]: t["text"] for t in catalog["source_texts"]}
    return selectable, catalog, by_id, texts, redundant


def challenge_projection(request):
    """Keep original verification material; omit duplicate assessor bookkeeping."""
    selectable, catalog, _by_id, texts, redundant = _catalog(request)
    aliases = {e.evidence_id: ref for ref, e in selectable.items()}
    sources = catalog["evidence"]
    for row in sources:
        bounds = row["excerpt_ref"]
        if bounds["start_char"] < bounds["end_char"]:
            row["span_bounds"] = _coordinates(
                texts[bounds["text_id"]], bounds["start_char"], bounds["end_char"]
            )
    numbered = [
        {
            "text_id": t["text_id"],
            "text_hash": t["text_hash"],
            "numbered_text": "".join(
                f"{i}|{line}"
                for i, line in enumerate(t["text"].splitlines(keepends=True), 1)
            ),
        }
        for t in catalog["source_texts"]
    ]

    def refs(items):
        return list(
            dict.fromkeys(
                aliases[r.evidence_id] for r in items if r.evidence_id in aliases
            )
        )

    candidates = []
    for candidate in request.candidates:
        issue, proposal = candidate.issue, candidate.proposal
        core = issue.core.model_dump(mode="json", exclude={"citations"})
        candidates.append(
            {
                "proposal_id": candidate.proposal_id,
                "unit_id": candidate.unit_id,
                "kind": issue.kind,
                "claim": issue.claim,
                "rule_alleged": issue.rule.statement,
                "core": core,
                "mechanism_alleged": issue.mechanism,
                "counterevidence_alleged": issue.counterevidence.statement,
                "local_impact_alleged": issue.consequence,
                "optional_consequences": [
                    c.model_dump(mode="json") for c in issue.potential_consequences
                ],
                "missing_facts": [
                    f.model_dump(mode="json") for f in issue.missing_facts
                ],
                "assessor_labels": {
                    "applicability": proposal.applicability,
                    "normative": proposal.normative,
                    "lifecycle": proposal.lifecycle,
                    "attribution": issue.attribution,
                },
                "source_claims": {
                    "rule": refs(issue.rule.citations),
                    "core": refs(issue.core.citations),
                    "counterevidence": refs(issue.counterevidence.citations),
                },
                "revision_claims": [
                    {
                        "revision": r.revision,
                        "behavior": r.behavior,
                        "sources": refs(r.citations),
                        "observation_ids": r.observation_ids,
                    }
                    for r in issue.revisions
                ],
            }
        )
    assessment = request.assessment
    records = json.loads(assessment.context_records_json)
    coverage = coverage_summary(
        assessment.limitations,
        assessment.revision.repository_id,
        relevant_ids=[d for u in assessment.units for d in u.delta_ids],
        relevant_paths=[u.path for u in assessment.units],
    )
    coverage.update(
        adr_retrieval=assessment.adr_retrieval,
        adr_catalog_complete=assessment.adr_catalog_complete,
    )
    return {
        "input_format": "dg-challenge-source-v1",
        "batch_id": request.batch_id,
        "revision": assessment.revision.model_dump(mode="json"),
        "context_hash": assessment.context_hash,
        "units": [
            {
                "unit_id": u.unit_id,
                "channel": u.channel,
                "path": u.path,
                "rule_id": u.rule_id,
                "permitted_sources": [
                    aliases[e] for e in u.evidence_ids if e in aliases
                ],
            }
            for u in assessment.units
        ],
        "policy_links": [
            {"requirement_id": r["requirement_id"], "adr_ids": r["adr_ids"]}
            for r in records["requirements"]
        ],
        "deltas": [
            {k: d[k] for k in ("delta_id", "path", "previous_path")}
            for d in records["deltas"]
        ],
        "authority": [a.model_dump(mode="json") for a in assessment.authority],
        "observations": [o.model_dump(mode="json") for o in assessment.observations],
        "policy_observations": [
            o.model_dump(mode="json") for o in assessment.policy_observations
        ],
        "source_anchors": [
            a.model_dump(mode="json") for a in assessment.source_anchors
        ],
        "coverage": coverage,
        "sources": sources,
        "source_texts": numbered,
        "projection": {
            "canonical_sources": len(assessment.evidence),
            "repeated_discovery_sources": redundant,
            "source_identities_preserved": True,
        },
        "candidates": candidates,
    }


def expand_challenge_sources(data):
    """Audit-only lossless reconstruction of the displayed original sources."""
    texts = {}
    for item in data["source_texts"]:
        if item["text_id"] in texts:
            raise ValueError("AMBIGUOUS_SOURCE_IDENTITY")
        lines = []
        for number, line in enumerate(
            item["numbered_text"].splitlines(keepends=True), 1
        ):
            prefix = f"{number}|"
            if not line.startswith(prefix):
                raise ValueError("INVALID_NUMBERED_SOURCE")
            lines.append(line[len(prefix) :])
        text = "".join(lines)
        if digest(text) != item["text_hash"]:
            raise ValueError("INVALID_NUMBERED_SOURCE")
        texts[item["text_id"]] = text
    restored, supplied = [], {}
    for row in data["sources"]:
        raw = dict(row)
        bounds = raw.pop("excerpt_ref")
        text = texts.get(bounds["text_id"])
        if text is None or not 0 <= bounds["start_char"] <= bounds["end_char"] <= len(
            text
        ):
            raise ValueError("SPAN_OUTSIDE_EVIDENCE")
        supplied[raw["evidence_id"]] = raw.pop("source_ref", None)
        raw.pop("span_bounds", None)
        raw.pop("read_only", None)
        raw["excerpt"] = text[bounds["start_char"] : bounds["end_char"]]
        restored.append(EvidenceSnapshot.model_validate_json(json.dumps(raw)))
    expected = {
        e.evidence_id: ref
        for ref, e in source_references(
            SimpleNamespace(
                evidence=restored, revision=SimpleNamespace(**data["revision"])
            )
        ).items()
    }
    if supplied != {e.evidence_id: expected.get(e.evidence_id) for e in restored}:
        raise ValueError("Source reference ownership mismatch")
    return tuple(restored)


def resolve_span(span, request, catalog=None):
    selectable, _, rows, texts, _ = catalog or _catalog(request)
    source = selectable.get(span.source_ref)
    if source is None:
        raise ValueError("UNKNOWN_SOURCE_REF")
    if span.span_id[0] != ROLE_PREFIX[span.source_ref.split(":", 1)[0]]:
        raise ValueError("SPAN_SOURCE_ROLE_MISMATCH")
    ref = rows[source.evidence_id]["excerpt_ref"]
    text = texts[ref["text_id"]]
    lines, starts = _lines(text)
    if not 1 <= span.start_line <= span.end_line <= len(lines):
        raise ValueError("INVALID_SOURCE_SPAN")
    last_column = (
        len(lines[span.end_line - 1]) if span.end_column is None else span.end_column
    )
    if not 0 <= span.start_column <= len(
        lines[span.start_line - 1]
    ) or not 0 <= last_column <= len(lines[span.end_line - 1]):
        raise ValueError("INVALID_SOURCE_SPAN")
    start = starts[span.start_line - 1] + span.start_column
    end = starts[span.end_line - 1] + last_column
    if not ref["start_char"] <= start < end <= ref["end_char"]:
        raise ValueError("SPAN_OUTSIDE_EVIDENCE")
    quote = text[start:end]
    if len(quote) > MAX_SPAN_CHARACTERS:
        raise ValueError("SPAN_TOO_LARGE")
    return QuoteRef(
        evidence_id=source.evidence_id,
        quote=quote,
        start_char=start - ref["start_char"],
        end_char=end - ref["start_char"],
    )


def expand_challenge(payload, request):
    catalog = _catalog(request)
    data = payload.model_dump(mode="json")
    definitions = data.pop("spans")
    pool, duplicate = {}, set()
    for span in payload.spans:
        if span.span_id in pool:
            duplicate.add(span.span_id)
        pool[span.span_id] = span
    accepted, rejected = [], []
    for decision in data["decisions"]:
        original = json.dumps({"decision": decision, "spans": definitions})
        try:

            def walk(value, role=None):
                if isinstance(value, dict):
                    if "revision" in value and "citations" in value:
                        role = value["revision"]
                    for key, entries in value.items():
                        if key == "citations":
                            resolved = []
                            for identifier in entries:
                                if identifier in duplicate:
                                    raise ValueError("DUPLICATE_SPAN_ID")
                                if identifier not in pool:
                                    raise ValueError("UNKNOWN_SPAN_ID")
                                span = pool[identifier]
                                ref = resolve_span(span, request, catalog)
                                source = catalog[0][span.source_ref]
                                if role and (
                                    source.kind != "CODE" or source.origin != role
                                ):
                                    raise ValueError("WRONG_REVISION_CITATION")
                                resolved.append(ref.model_dump(mode="json"))
                            value[key] = resolved
                        else:
                            walk(entries, role)
                elif isinstance(value, list):
                    for entry in value:
                        walk(entry, role)

            walk(decision)
            accepted.append(ChallengeDecision.model_validate_json(json.dumps(decision)))
        except ValueError as error:
            reason = (
                "INVALID_REVIEW_SCHEMA"
                if isinstance(error, ValidationError)
                else str(error)
            )
            rejected.append(
                ResponseRejection(
                    subject_id=decision["proposal_id"], reason=reason, raw_json=original
                )
            )
    return ChallengeBatchOutput(
        batch_id=payload.batch_id, decisions=tuple(accepted), rejections=tuple(rejected)
    )


def compact_challenge(output, request):
    """Offline helper: preserve exact original source selections as spans."""
    from .wire import WireChallenge

    selectable, _, rows, texts, _ = _catalog(request)
    aliases = {e.evidence_id: ref for ref, e in selectable.items()}
    evidence = {e.evidence_id: e for e in selectable.values()}
    data, pool, spans = output.model_dump(mode="json"), {}, []
    if data.pop("rejections", ()):
        raise ValueError("Cannot compact rejected review as a new response")

    def walk(value):
        if isinstance(value, dict):
            for key, entries in value.items():
                if key == "citations":
                    ids = []
                    for item in entries:
                        ref = QuoteRef.model_validate_json(json.dumps(item))
                        citation = resolve_quote(ref, evidence)
                        source_ref = aliases[ref.evidence_id]
                        identity = (
                            ref.evidence_id,
                            citation.start_char,
                            citation.end_char,
                        )
                        if identity not in pool:
                            identifier = ROLE_PREFIX[source_ref.split(":", 1)[0]] + str(
                                len(pool) + 1
                            )
                            bounds = rows[ref.evidence_id]["excerpt_ref"]
                            coordinates = _coordinates(
                                texts[bounds["text_id"]],
                                bounds["start_char"] + citation.start_char,
                                bounds["start_char"] + citation.end_char,
                            )
                            pool[identity] = identifier
                            spans.append(
                                {
                                    "span_id": identifier,
                                    "source_ref": source_ref,
                                    **coordinates,
                                }
                            )
                        ids.append(pool[identity])
                    value[key] = ids
                else:
                    walk(entries)
        elif isinstance(value, list):
            for entry in value:
                walk(entry)

    walk(data)
    data.update(schema_version="dg-challenge-spans-v1", spans=spans)
    return WireChallenge.model_validate_json(json.dumps(data))
