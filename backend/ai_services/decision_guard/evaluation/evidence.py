"""Exact source reads and quote resolution; no architectural interpretation."""

import asyncio
import json

from ..models import (
    ArchitectureContext,
    EvidenceSnapshot,
    Limitation,
    SourceLocation,
    digest,
    stable_id,
)
from ..source import validate_path
from .models import Citation, EvaluationInputError, EvidenceBundle, SourceObservation


def validate_context(context):
    try:
        ctx = ArchitectureContext.model_validate_json(context.model_dump_json())
        payload = ctx.model_dump(mode="json", exclude={"context_hash", "budget"})
        hashes = {digest(json.dumps(payload, sort_keys=True, ensure_ascii=False))}
        if ctx.context_builder_version == "1":
            # Original v1 predates applicability fields; never hash v2 defaults as v1.
            for r in payload["requirements"]:
                for key in (
                    "declared_applicability_json",
                    "scope_metadata_status",
                    "enrichment_fingerprint",
                    "enrichment_status",
                    "enrichment_model",
                    "supported_community_ids",
                    "scope_evidence_ids",
                ):
                    r.pop(key, None)
            for adr in payload["adr_snapshot"]:
                adr.pop("applicability_fingerprint", None)
            hashes.add(digest(json.dumps(payload, sort_keys=True, ensure_ascii=False)))
        if ctx.context_hash not in hashes:
            raise ValueError("Context hash mismatch")
        catalog = [a.model_dump() for a in ctx.adr_snapshot]
        catalog_hashes = {digest(json.dumps(catalog, sort_keys=True))}
        if ctx.context_builder_version == "1":
            catalog_hashes.add(
                digest(
                    json.dumps(
                        [
                            {
                                k: v
                                for k, v in a.items()
                                if k != "applicability_fingerprint"
                            }
                            for a in catalog
                        ],
                        sort_keys=True,
                    )
                )
            )
        if ctx.adr_catalog_hash not in catalog_hashes:
            raise ValueError("ADR catalog hash mismatch")
        rev = ctx.revision
        if rev.revision_key != digest(
            f"{rev.github_repository_id}:{rev.pull_request_number}:{rev.base_sha}:{rev.head_sha}"
        ):
            raise ValueError("Revision key mismatch")
        roles = role_shas(ctx)
        catalog = {a.adr_id: a.content_hash for a in ctx.adr_snapshot}
        for e in ctx.evidence:
            if (
                e.kind == "CODE"
                and e.origin in roles
                and e.commit_sha != roles[e.origin]
            ):
                raise ValueError("Evidence revision mismatch")
            if e.path:
                validate_path(e.path)
            if e.kind == "ADR" and catalog.get(e.adr_id) != e.document_hash:
                raise ValueError("ADR evidence escaped document snapshot")
        for seq, key in (
            (ctx.requirements, "requirement_id"),
            (ctx.relationships, "relationship_id"),
        ):
            if len({getattr(x, key) for x in seq}) != len(seq):
                raise ValueError("Duplicate candidate ID")
        return ctx
    except (ValueError, TypeError, AttributeError) as exc:
        raise EvaluationInputError(str(exc)) from None


def role_shas(ctx):
    return {
        "MERGE_BASE": ctx.revision.merge_base_sha,
        "TARGET_BASE": ctx.revision.base_sha,
        "PR_HEAD": ctx.revision.head_sha,
    }


def resolve_quote(ref, evidence):
    e = evidence.get(ref.evidence_id)
    if e is None or e.completeness == "UNAVAILABLE":
        raise ValueError("UNKNOWN_EVIDENCE")
    if ref.start_char is not None:
        start, end = ref.start_char, ref.end_char
        if not 0 <= start < end <= len(e.excerpt) or e.excerpt[start:end] != ref.quote:
            raise ValueError("INVALID_SOURCE_SPAN")
    else:
        start = e.excerpt.find(ref.quote)
        if start < 0:
            raise ValueError("QUOTE_NOT_FOUND")
        if e.excerpt.find(ref.quote, start + 1) >= 0:
            raise ValueError("QUOTE_AMBIGUOUS")
        end = start + len(ref.quote)
    location = None
    if e.kind == "CODE" and e.location and e.placement == "EXACT":
        first = e.location.start_line + e.excerpt[:start].count("\n")
        last = e.location.start_line + e.excerpt[: end - 1].count("\n")
        if last > e.location.end_line:
            raise ValueError("CITATION_OUTSIDE_LOCATION")
        location = SourceLocation(path=e.path, start_line=first, end_line=last)
    return Citation(
        evidence_id=e.evidence_id,
        quote=ref.quote,
        start_char=start,
        end_char=end,
        location=location,
    )


class SourceSupplementer:
    """Per-run bounded cache. Reader must provide immutable Git-object reads.

    No live graph reads occur, so generations cannot be mixed. The host owns a
    clone lease if cleanup can race reads. Source reader's own I/O must be bounded.
    """

    def __init__(self, source, settings):
        self.source, self.settings = source, settings
        self.cache, self.evidence, self.observations, self.limitations = {}, {}, {}, []
        self.reads = self.bytes = 0

    async def read(self, ctx, path, role):
        validate_path(path)
        sha = role_shas(ctx)[role]
        key = sha, path
        oid = stable_id(ctx.context_hash, role, path, "observation")
        if oid in self.observations:
            return
        if key not in self.cache:
            if (
                self.reads >= self.settings.max_reads
                or self.bytes >= self.settings.max_source_bytes
            ):
                self.limitations.append(
                    Limitation(
                        code="SOURCE_READ_BUDGET",
                        message="Supplement budget exhausted",
                        paths=(path,),
                    )
                )
                return
            self.reads += 1
            task = asyncio.create_task(
                asyncio.to_thread(
                    self.source.read_blob, ctx.revision.repository_id, sha, path
                )
            )
            try:
                blob = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Drain object read before host releases its clone lease.
                await task
                raise
            except Exception:  # noqa: BLE001 -- source port failures become explicit coverage gaps
                self.limitations.append(
                    Limitation(
                        code="SOURCE_READ_FAILED",
                        message="Exact source unavailable",
                        paths=(path,),
                    )
                )
                return
            if (blob.repository_id, blob.commit_sha, blob.path) != (
                ctx.revision.repository_id,
                sha,
                path,
            ):
                raise EvaluationInputError(
                    "Supplement escaped requested source identity"
                )
            if (blob.status == "complete") != (blob.text is not None):
                raise EvaluationInputError("Source status/text mismatch")
            self.cache[key] = blob
        blob = self.cache[key]
        status = (
            "ABSENT"
            if blob.status == "absent"
            else "PRESENT"
            if blob.status == "complete"
            else "UNAVAILABLE"
        )
        eid = None
        if status == "PRESENT":
            size = len(blob.text.encode("utf-8"))
            if (
                size > self.settings.max_read_bytes
                or self.bytes + size > self.settings.max_source_bytes
            ):
                status = "UNAVAILABLE"
                self.limitations.append(
                    Limitation(
                        code="SOURCE_SIZE_LIMIT",
                        message="Whole source exceeds supplement budget",
                        paths=(path,),
                    )
                )
            else:
                self.bytes += size
                eid = stable_id(ctx.context_hash, role, path, digest(blob.text))
                location = (
                    SourceLocation(
                        path=path,
                        start_line=1,
                        end_line=max(1, len(blob.text.splitlines())),
                    )
                    if blob.text
                    else None
                )
                self.evidence[eid] = EvidenceSnapshot(
                    evidence_id=eid,
                    repository_id=ctx.revision.repository_id,
                    kind="CODE",
                    origin=role,
                    path=path,
                    commit_sha=sha,
                    excerpt=blob.text,
                    content_hash=digest(blob.text),
                    excerpt_hash=digest(blob.text),
                    completeness="COMPLETE",
                    placement="EXACT",
                    location=location,
                )
        self.observations[oid] = SourceObservation(
            observation_id=oid,
            revision=role,
            path=path,
            status=status,
            complete_file=status != "UNAVAILABLE",
            evidence_id=eid,
        )

    def bundle(self):
        return EvidenceBundle(
            evidence=tuple(self.evidence.values()),
            observations=tuple(self.observations.values()),
            limitations=tuple(self.limitations),
        )
