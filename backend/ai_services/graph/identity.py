from __future__ import annotations


def clean_entity_id(raw_id: str | None) -> str:
    """Normalize raw entity ID by stripping leading/trailing whitespace and 'entity:' prefix."""
    if not raw_id:
        return ""
    val = str(raw_id).strip()
    return val.removeprefix("entity:")


def entity_graph_id(canonical_id: str | None) -> str:
    """Generate canonical frontend node ID matching GET /repositories/{repo_id}/graph.

    Format: 'entity:<canonical_id>'
    """
    clean_id = clean_entity_id(canonical_id)
    if not clean_id:
        return ""
    return f"entity:{clean_id}"


def edge_graph_id(
    source_canonical_id: str | None,
    relationship_type: str | None,
    target_canonical_id: str | None,
) -> str:
    """Generate canonical frontend edge ID matching GET /repositories/{repo_id}/graph.

    Format: 'edge:<source_clean>:<relationship_type>:<target_clean>'
    """
    src_clean = clean_entity_id(source_canonical_id)
    tgt_clean = clean_entity_id(target_canonical_id)
    rel = str(relationship_type or "").strip()

    if not src_clean or not tgt_clean or not rel or src_clean == tgt_clean:
        return ""
    return f"edge:{src_clean}:{rel}:{tgt_clean}"


def clean_assertion_id(raw_id: str | None) -> str:
    """Normalize raw assertion ID by stripping leading/trailing whitespace and 'assertion:' prefix."""
    if not raw_id:
        return ""
    val = str(raw_id).strip()
    return val.removeprefix("assertion:")


def assertion_graph_id(raw_assertion_id: str | None) -> str:
    """Generate canonical assertion provenance ID.

    Format: 'assertion:<clean_assertion_id>'
    e.g., 'assertion:adr:...'
    """
    clean_id = clean_assertion_id(raw_assertion_id)
    if not clean_id:
        return ""
    return f"assertion:{clean_id}"
