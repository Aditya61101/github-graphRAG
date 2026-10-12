"""Read persisted ADR applicability using its existing schema, without assessment."""

import json
import re
from dataclasses import dataclass

from ai_services.ingestion.adr.applicability import (
    ApplicabilityMetadata,
    applicability_properties,
)
from ai_services.ingestion.adr.scope_resolver import ADRScopeResolver

from .models import digest


@dataclass(frozen=True)
class ScopeRecord:
    metadata_json: str | None
    status: str
    fingerprint: str
    enrichment_status: str | None
    model: str | None

    @property
    def metadata(self):
        return (
            ApplicabilityMetadata.from_json(self.metadata_json)
            if self.status == "VALID"
            else ApplicabilityMetadata()
        )


def read_scope(properties: dict) -> ScopeRecord:
    raw = properties.get("applicability_json")
    status = "LEGACY" if raw is None else "VALID"
    canonical = raw
    if raw is not None:
        try:
            canonical = ApplicabilityMetadata.from_json(raw).to_json()
        except (ValueError, TypeError):
            status = "INVALID"
            canonical = str(raw)
    fingerprint_data = {
        k: v for k, v in properties.items() if k.startswith("applicability_")
    }
    if status == "VALID":
        fingerprint_data["applicability_json"] = json.loads(canonical)
    return ScopeRecord(
        canonical,
        status,
        digest(json.dumps(fingerprint_data, sort_keys=True, default=str)),
        properties.get("applicability_status"),
        properties.get("applicability_model"),
    )


def scope_chunk_ids(metadata: ApplicabilityMetadata) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *metadata.applicability.evidence_chunk_ids,
                *(
                    cid
                    for target in metadata.applicability.targets
                    for cid in target.evidence_chunk_ids
                ),
            }
        )
    )


def supported_communities(metadata, entities, communities, owned_evidence_ids):
    # Reuse enrichment's exact semantic/membership/basis checks. An entity mention
    # cannot acquire subsystem-wide scope through MEMBER_OF alone.
    app = metadata.applicability.model_copy(deep=True)
    app.targets = [
        target
        for target in app.targets
        if set(target.evidence_chunk_ids) <= owned_evidence_ids
    ]
    resolved = ADRScopeResolver.resolve(
        app, entities, communities, context_truncated=metadata.context_truncated
    )
    return tuple(sorted(set(metadata.community_ids) & set(resolved.community_ids)))


def words(text: str) -> set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {
        part.casefold()
        for part in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text.replace("_", " "))
    }


def discovery(
    scope, node, targets, seeds, supported_ids, *, adr_mention, tokens, languages, paths
):
    metadata = scope.metadata
    app = metadata.applicability
    reasons = []
    score = 0.0
    if any(t.get("id") in seeds for t in targets):
        reasons.append("ENTITY_TARGET")
        score += 100
    if set(metadata.entity_ids) & set(seeds):
        reasons.append("SCOPE_ENTITY_TARGET")
        score += 80
    if supported_ids:
        reasons.append("COMMUNITY_SCOPE")
        score += 70
    if "repository" in app.scopes:
        reasons.append("REPOSITORY_SCOPE")
        score += 65
    for condition in app.conditions:
        if (
            (
                condition.kind == "language"
                and any(s.casefold() in languages for s in condition.selectors)
            )
            or (
                condition.kind == "directory"
                and any(
                    path.startswith(s.rstrip("/") + "/")
                    for path in paths
                    for s in condition.selectors
                )
            )
            or (
                condition.kind == "file_category"
                and any(s.casefold() in tokens for s in condition.selectors)
            )
        ):
            reasons.append("CONDITIONAL_SCOPE")
            score += 60
            break
    if adr_mention:
        reasons.append("ADR_MENTION")
        score += 50
    overlap = tokens & words(
        (node.get("title") or "") + " " + (node.get("description") or "")
    )
    if overlap:
        reasons.append("LEXICAL_FALLBACK")
        score += min(len(overlap), 20)
    if not reasons:
        # Inferred/explicit mismatches are ranking hints, never exclusion proofs.
        if scope.status == "LEGACY":
            reasons.append("LEGACY_SCOPE_FALLBACK")
        elif scope.status == "INVALID":
            reasons.append("INVALID_SCOPE_FALLBACK")
        elif applicability_properties(metadata, scope.model or "unknown")[
            "applicability_unresolved"
        ]:
            reasons.append("UNCERTAIN_SCOPE_FALLBACK")
        else:
            reasons.append("SCOPE_MISMATCH_FALLBACK")
    # Preserve original discovery semantics for linked/lexical legacy records.
    if "ENTITY_TARGET" in reasons and scope.status == "LEGACY":
        reasons = ["ENTITY_TARGET"]
    elif adr_mention and scope.status == "LEGACY":
        reasons = ["ADR_MENTION"]
    return tuple(dict.fromkeys(reasons)), score
