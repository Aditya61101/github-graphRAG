"""Read-only prototype authority from trusted repository ingestion records."""

from pathlib import Path

from ..models import EvidenceSnapshot, Limitation, digest, stable_id
from .models import AuthorityReceipt, EvidenceBundle, PolicyObservation


class UploadAuthority:
    """SQLite is a trusted backend store, not a client-supplied authority claim.

    Owner-only uploads are an established product assumption. Completed ADRs
    associated with this repository are approved document-level policy sources;
    source IDs and uploader markers are irrelevant to that approval.
    """

    def __init__(self, store, storage_dir, max_document_bytes=10 * 1024 * 1024):
        self.store = store
        self.storage_dir = Path(storage_dir).resolve()
        self.max_document_bytes = max_document_bytes

    def receipts(self, context):
        repo = self.store.get_repository(context.revision.repository_id)
        result = []
        for doc in context.adr_snapshot:
            record = self.store.get_adr(doc.adr_id)
            owner = str(repo.user_id) if repo and repo.user_id else "unknown"
            reason = "REPOSITORY_ADR_UNVERIFIED"
            approved = bool(
                repo
                and repo.id == context.revision.repository_id
                and record
                and record.repository_id == repo.id
                and record.id == doc.adr_id
                and record.content_hash == doc.content_hash
                and record.status == "COMPLETED"
            )
            if approved:
                try:
                    path = Path(record.file_path).resolve()
                    if not path.is_relative_to(self.storage_dir / repo.id / record.id):
                        raise ValueError("Untrusted upload path")
                    with path.open("rb") as f:
                        data = f.read(self.max_document_bytes + 1)
                    import hashlib

                    approved = (
                        len(data) <= self.max_document_bytes
                        and hashlib.sha256(data).hexdigest() == doc.content_hash
                    )
                    reason = (
                        "REPOSITORY_ADR_VERIFIED"
                        if approved
                        else "UPLOAD_HASH_MISMATCH"
                    )
                except (OSError, ValueError):
                    approved, reason = False, "UPLOAD_SOURCE_UNAVAILABLE"
            result.append(
                AuthorityReceipt(
                    repository_id=context.revision.repository_id,
                    adr_id=doc.adr_id,
                    document_hash=doc.content_hash,
                    owner_id=owner,
                    status="APPROVED" if approved else "UNKNOWN",
                    reason=reason,
                )
            )
        return tuple(result)

    def policy_evidence(self, context, *, adr_ids, settings):
        """Bounded read-only original text; never treat a chunk as a whole ADR.

        Binary-document text helps review, but the current parser cannot attest
        image/table/blank-page coverage, so PDF/DOCX remain explicitly partial.
        """
        import hashlib

        from ...ingestion.adr.parser import ADRParseError, parse_adr_content

        repo = self.store.get_repository(context.revision.repository_id)
        evidence, observations, limitations = [], [], []
        reads = size = text_size = 0
        for doc in context.adr_snapshot:
            if doc.adr_id not in adr_ids:
                continue
            try:
                if reads >= settings.max_policy_reads:
                    raise ValueError("POLICY_READ_LIMIT")
                reads += 1
                record = self.store.get_adr(doc.adr_id)
                if (
                    not repo
                    or repo.id != context.revision.repository_id
                    or not record
                    or (
                        record.id,
                        record.repository_id,
                        record.content_hash,
                        record.status,
                    )
                    != (doc.adr_id, repo.id, doc.content_hash, "COMPLETED")
                ):
                    raise ValueError("POLICY_IDENTITY_MISMATCH")
                path = Path(record.file_path).resolve()
                if not path.is_relative_to(self.storage_dir / repo.id / record.id):
                    raise ValueError("POLICY_PATH_MISMATCH")
                limit = min(
                    self.max_document_bytes,
                    settings.max_policy_read_bytes,
                    settings.max_policy_bytes - size,
                )
                if limit <= 0:
                    raise ValueError("POLICY_SIZE_LIMIT")
                with path.open("rb") as stream:
                    data = stream.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("POLICY_SIZE_LIMIT")
                size += len(data)
                if hashlib.sha256(data).hexdigest() != doc.content_hash:
                    raise ValueError("POLICY_HASH_MISMATCH")
                parsed = parse_adr_content(
                    data,
                    path.suffix,
                    getattr(record, "source_name", None) or path.name,
                    doc.adr_id,
                    repo.id,
                    str(path),
                    doc.content_hash,
                    source_type=getattr(record, "source_type", None) or "UNKNOWN",
                    source_id=getattr(record, "source_id", None),
                    source_url=getattr(record, "source_url", None),
                    source_version=getattr(record, "source_version", None),
                )
                text = parsed.content
                amount = len(text.encode("utf-8"))
                if (
                    amount > settings.max_policy_read_bytes
                    or text_size + amount > settings.max_policy_bytes
                ):
                    raise ValueError("POLICY_TEXT_SIZE_LIMIT")
                text_size += amount
                complete = path.suffix.lower() in {".md", ".markdown", ".txt"}
                eid = stable_id(
                    context.context_hash,
                    "policy-text-v1",
                    doc.adr_id,
                    doc.content_hash,
                    digest(text),
                )
                evidence.append(
                    EvidenceSnapshot(
                        evidence_id=eid,
                        repository_id=repo.id,
                        kind="ADR",
                        origin="ADR_DOCUMENT",
                        adr_id=doc.adr_id,
                        document_hash=doc.content_hash,
                        chunk_id="policy-text-v1:" + doc.adr_id,
                        source_name=parsed.source_name,
                        source_type=parsed.source_type,
                        source_url=parsed.source_url,
                        source_version=parsed.source_version,
                        section="Original document text",
                        excerpt=text,
                        excerpt_hash=digest(text),
                        content_hash=digest(text),
                        completeness="COMPLETE" if complete else "EXCERPT",
                        placement="NOT_APPLICABLE",
                    )
                )
                observations.append(
                    PolicyObservation(
                        adr_id=doc.adr_id,
                        document_hash=doc.content_hash,
                        evidence_id=eid,
                        coverage="TEXT_COMPLETE" if complete else "TEXT_PARTIAL",
                    )
                )
                if not complete:
                    limitations.append(
                        Limitation(
                            code="POLICY_TEXT_PARTIAL",
                            message="Binary ADR text extraction does not attest complete visual content",
                            item_ids=(doc.adr_id,),
                        )
                    )
            except (OSError, ValueError, ADRParseError):
                limitations.append(
                    Limitation(
                        code="POLICY_SOURCE_UNAVAILABLE",
                        message="Original policy text could not be verified within read limits",
                        item_ids=(doc.adr_id,),
                    )
                )
        return EvidenceBundle(
            evidence=tuple(evidence),
            policy_observations=tuple(observations),
            limitations=tuple(limitations),
        )


def authority_hash(receipts):
    return digest(
        "repository-adr-v1:"
        + "|".join(
            r.model_dump_json() for r in sorted(receipts, key=lambda r: r.adr_id)
        )
    )
