"""Verified original ADR chunk vectors and SUPPORTS projection; no embeddings generated."""

import hashlib
import json
import time
from pathlib import Path

from pydantic import ValidationError

from ...ingestion.adr.chunker import ADRChunker
from ...ingestion.adr.parser import parse_adr_content
from ..context import _scope
from .models import ADRChunkVector, StoredVector, vector_key


class UploadedADRChunkVerifier:
    """Read original approved uploads through the existing parser/chunker, bounded once/doc."""

    def __init__(self, authority, max_reads, max_bytes, deadline):
        self.authority = authority
        self.max_reads, self.remaining, self.deadline = max_reads, max_bytes, deadline
        self.documents = {}

    def __call__(self, support):
        a, c = support["adr"], support["chunk"]
        key = a["repository"], a["id"], a["content_hash"]
        if key not in self.documents:
            if (
                time.monotonic() >= self.deadline
                or len(self.documents) >= self.max_reads
                or self.remaining <= 0
            ):
                raise ValueError("ADR chunk source verification budget")
            self.documents[key] = {}
            record = self.authority.store.get_adr(a["id"])
            if not record or (
                record.repository_id,
                record.content_hash,
                record.status,
            ) != (key[0], key[2], "COMPLETED"):
                raise ValueError("ADR chunk upload identity changed")
            path = Path(record.file_path).resolve()
            if not path.is_relative_to(self.authority.storage_dir / key[0] / key[1]):
                raise ValueError("ADR chunk upload escaped storage")
            cap = min(self.remaining, self.authority.max_document_bytes)
            with path.open("rb") as stream:
                data = stream.read(cap + 1)
            if len(data) > cap or hashlib.sha256(data).hexdigest() != key[2]:
                raise ValueError("ADR chunk original upload hash/size mismatch")
            self.remaining -= len(data)
            doc = parse_adr_content(
                data, path.suffix, record.source_name, key[1], key[0], str(path), key[2]
            )
            self.documents[key] = {
                chunk.chunk_id: chunk for chunk in ADRChunker().chunk(doc)
            }
        original = self.documents[key].get(c["id"])
        return (
            original is not None
            and original.text == c.get("text")
            and original.content_hash == c.get("contentHash")
        )


def collect_chunk_vectors(
    rows, *, repo, catalog, approved, units, resolve, verifier, manifest, note
):
    catalog_hashes = {a.adr_id: a.content_hash for a in catalog}
    requirements = {
        u.original_id: u.requirement for u in units.values() if u.requirement
    }
    result, seen = [], set()
    for row in rows:
        a, c = row["adr"], row["chunk"]
        _scope(a, repo)
        _scope(c, repo)
        if c.get("adr_id") != a["id"] or catalog_hashes.get(a["id"]) != a.get(
            "content_hash"
        ):
            raise ValueError("ADR chunk escaped defining document/catalog")
        if c["id"] in seen:
            raise ValueError("Duplicate original ADR chunk vector")
        seen.add(c["id"])
        if a["id"] not in approved:
            continue
        value = resolve(row)
        if not value:
            note(
                "ADR_CHUNK_SOURCE_REJECTED",
                "Stored chunk hash/source identity failed",
                ids=(c["id"],),
            )
            continue
        try:
            if not verifier(row):
                raise ValueError(
                    "Chunk text does not reproduce from the original upload"
                )
            primary = []
            for target in row.get("requirements", ()):
                _scope(target, repo)
                if target.get("adr_id") != a["id"]:
                    raise ValueError("SUPPORTS target escaped defining ADR")
                rid = target["id"]
                if rid not in requirements:
                    note(
                        "SEMANTIC_MAPPING_REQUIREMENT_OMITTED",
                        "SUPPORTS target is outside the bounded verified requirement inventory",
                        ids=(c["id"], rid),
                    )
                    continue
                if value.evidence_id not in requirements[rid].evidence_ids:
                    raise ValueError(
                        "SUPPORTS disagrees with original requirement support"
                    )
                primary.append(rid)
            fields = (
                "embedding_model",
                "embedding_provider",
                "embedding_dimensions",
                "embedding_revision",
                "embedding_text_hash",
            )
            observed = {k: c[k] for k in fields if c.get(k) is not None}
            basis = (
                "MOCK_FIXTURE"
                if manifest.confirmation == "MOCK_FIXTURE"
                else "NEO4J_METADATA_VERIFIED"
                if len(observed) == len(fields)
                else "LEGACY_CONFIRMED"
            )
            data = {
                "repository_id": repo,
                "adr_id": a["id"],
                "document_hash": a["content_hash"],
                "chunk_id": c["id"],
                "content_hash": c["contentHash"],
                "evidence_id": value.evidence_id,
                "requirement_ids": tuple(sorted(set(primary))),
                "metadata_basis": basis,
                "observed_metadata_json": json.dumps(observed, sort_keys=True),
                "verified_metadata_fields": tuple(sorted(observed)),
            }
            stub = ADRChunkVector.model_construct(**data)
            data["vector"] = StoredVector(
                identity=manifest.identity,
                key=vector_key(stub, manifest.identity),
                values=tuple(c.get("embedding") or ()),
            )
            result.append(ADRChunkVector(**data))
        except (ValueError, TypeError, AttributeError, OSError, ValidationError) as exc:
            note(
                "ADR_CHUNK_VECTOR_REJECTED",
                type(exc).__name__ + ": source/vector/metadata validation failed",
                ids=(c["id"],),
            )
    return tuple(sorted(result, key=lambda c: c.chunk_id))
