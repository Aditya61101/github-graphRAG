"""Shared original graph/source validators used by context and derived indexing."""

from .delta import clipped, code_evidence
from .models import EvidenceSnapshot, digest, stable_id
from .source import validate_path


def original_evidence_resolvers(
    repo,
    settings,
    source,
    target_sha,
    evidence,
    baseline_verified_evidence,
    limitation,
    scope,
):
    def chunk_evidence(chunk):
        scope(chunk, repo)
        text, path, sha = (
            chunk.get("text"),
            chunk.get("filePath"),
            chunk.get("commit"),
        )
        if text is None or not path or not sha:
            limitation(
                "CODE_EVIDENCE_INCOMPLETE",
                "Chunk lacks source identity or text",
                ids=(chunk.get("id", "unknown"),),
            )
            return None
        validate_path(path)
        placement, start = "UNLOCATED", None
        if chunk.get("contentHash") != digest(text):
            limitation(
                "CHUNK_HASH_MISMATCH",
                "Stored chunk hash disagrees with original text",
                (path,),
                (chunk["id"],),
            )
        else:
            try:
                blob = source.read(sha, path)
                if blob.status == "complete":
                    occurrences = blob.text.count(text) if text else 0
                    if occurrences == 1:
                        placement = "EXACT"
                        start = blob.text[: blob.text.index(text)].count("\n") + 1
                    elif occurrences > 1:
                        placement = "AMBIGUOUS"
            except Exception as exc:  # noqa: BLE001 - source port failures become sanitized coverage diagnostics
                limitation(
                    "INDEXED_SOURCE_UNAVAILABLE",
                    type(exc).__name__,
                    (path,),
                    (chunk["id"],),
                )
        if placement != "EXACT":
            limitation(
                "CHUNK_" + placement,
                "Chunk cannot be uniquely placed in exact source",
                (path,),
                (chunk["id"],),
            )
        ev = code_evidence(
            repo,
            "INDEXED_CODE",
            path,
            sha,
            text,
            settings,
            start=start,
            chunk_id=chunk["id"],
            placement=placement,
            content_hash=chunk.get("contentHash"),
        )
        evidence[ev.evidence_id] = ev
        try:
            target = source.read(target_sha, path)
            if (
                placement == "EXACT"
                and target.text is not None
                and text
                and target.text.count(text) == 1
            ):
                baseline_verified_evidence.add(ev.evidence_id)
            else:
                limitation(
                    "BASELINE_CHUNK_DIVERGED",
                    "Stored chunk is not uniquely corroborated in target-base source",
                    (path,),
                    (chunk["id"],),
                )
        except Exception as exc:  # noqa: BLE001 - source port failures become sanitized coverage diagnostics
            limitation(
                "BASELINE_SOURCE_UNAVAILABLE",
                type(exc).__name__,
                (path,),
                (chunk["id"],),
            )
        if ev.completeness == "EXCERPT":
            limitation(
                "EVIDENCE_LIMIT",
                "Code evidence was excerpted",
                (path,),
                (ev.evidence_id,),
            )
        return ev

    def adr_evidence(support):
        chunk, adr = support.get("chunk"), support.get("adr")
        if not chunk or not adr:
            limitation(
                "ADR_EVIDENCE_MISSING",
                "Requirement/assertion has no original supporting ADR chunk",
            )
            return None
        scope(chunk, repo)
        scope(adr, repo)
        if (
            chunk.get("adr_id") != adr.get("id")
            or not adr.get("content_hash")
            or chunk.get("text") is None
        ):
            limitation(
                "ADR_PROVENANCE_INCOMPLETE",
                "ADR chunk/document identity is incomplete",
            )
            return None
        if chunk.get("contentHash") != digest(chunk["text"]):
            limitation(
                "ADR_HASH_MISMATCH",
                "ADR supporting text hash mismatch",
                ids=(chunk.get("id", "unknown"),),
            )
            return None
        text = clipped(chunk["text"], settings.max_evidence_chars)
        ev = EvidenceSnapshot(
            evidence_id=stable_id(
                repo,
                "ADR",
                adr["id"],
                adr["content_hash"],
                chunk["id"],
                digest(text),
            ),
            repository_id=repo,
            kind="ADR",
            origin="ADR_DOCUMENT",
            adr_id=adr["id"],
            chunk_id=chunk["id"],
            document_hash=adr["content_hash"],
            content_hash=chunk["contentHash"],
            section=chunk.get("section"),
            source_name=adr.get("source_name"),
            source_type=adr.get("source_type"),
            source_url=adr.get("source_url"),
            source_version=adr.get("source_version"),
            excerpt=text,
            excerpt_hash=digest(text),
            completeness="EXCERPT" if len(text) < len(chunk["text"]) else "COMPLETE",
            placement="NOT_APPLICABLE",
        )
        evidence[ev.evidence_id] = ev
        if ev.completeness == "EXCERPT":
            limitation(
                "ADR_EVIDENCE_LIMIT",
                "Original ADR text was excerpted",
                ids=(ev.evidence_id,),
            )
        return ev

    return chunk_evidence, adr_evidence
