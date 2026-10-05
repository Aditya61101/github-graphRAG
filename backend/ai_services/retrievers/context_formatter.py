def _format_entity(entity: dict) -> str:
    return (
        f"{entity.get('label') or 'Entity'}: {entity.get('name') or '(unnamed)'} "
        f"(canonical_id={entity.get('canonical_id') or '(unknown)'})"
    )


def format_graph_context(graph_records: list[dict]) -> str:
    lines = []
    for record in graph_records:
        lines.append(
            "- "
            + _format_entity({
                "canonical_id": record.get("source_id"),
                "label": record.get("source_label"),
                "name": record.get("source"),
            })
            + f" -[{record['relationship']}]-> "
            + _format_entity({
                "canonical_id": record.get("target_id"),
                "label": record.get("target_label"),
                "name": record.get("target"),
            })
        )
    return "\n".join(lines)


def _format_evidence(evidence: dict, prefix: str = "") -> str:
    path = evidence.get("file_path") or "(unknown file)"
    chunk_id = evidence.get("chunk_id") or "(unknown chunk)"
    score = evidence.get("score")
    score_text = f", similarity={score:.4f}" if isinstance(score, (int, float)) else ""
    return (
        f"{prefix}Source: {path} [chunk_id={chunk_id}{score_text}]\n"
        f"```text\n{evidence.get('excerpt') or evidence.get('text') or ''}\n```"
    )


def format_retrieval_context(
    entity_results,
    community_results,
    communities,
    graph_records,
    chunk_results=None,
    entity_evidence=None,
    relationship_evidence=None,
):
    sections = []

    if entity_results.items:
        lines = []
        for item in entity_results.items:
            score = item.metadata.get("score")
            lines.append(f"- {_format_entity(item.metadata)} (similarity={score:.4f})")
        sections.append("## Relevant Entities\n" + "\n".join(lines))

    if community_results.items:
        lines = []
        for item in community_results.items:
            community_id = item.metadata["community_id"]
            score = item.metadata.get("score")
            lines.append(
                f"### Community {community_id} (similarity={score:.4f})\n{item.content}"
            )
        sections.append("## Relevant Communities\n" + "\n\n".join(lines))

    if communities:
        lines = []
        for community in communities:
            community_id = community["community_id"]
            members = community["members"]
            relationships = community["relationships"]
            summary = community.get("summary") or "(no persisted summary)"
            member_lines = [f"  - {_format_entity(member)}" for member in members]
            relationship_lines = [
                "  - "
                + _format_entity({
                    "canonical_id": relationship["source_id"],
                    "label": relationship["source_label"],
                    "name": relationship["source_name"],
                })
                + f" -[{relationship['relationship']}]-> "
                + _format_entity({
                    "canonical_id": relationship["target_id"],
                    "label": relationship["target_label"],
                    "name": relationship["target_name"],
                })
                for relationship in relationships
            ]
            lines.append(
                f"### Community {community_id}\n"
                f"Summary: {summary}\n"
                "Members:\n"
                + ("\n".join(member_lines) if member_lines else "  - (none)")
                + "\nInternal Directed Relationships:\n"
                + ("\n".join(relationship_lines) if relationship_lines else "  - (none)")
            )
        sections.append("## Community Details\n" + "\n\n".join(lines))

    graph_context = format_graph_context(graph_records)
    if graph_context:
        sections.append("## Graph Relationships\n" + graph_context)

    if chunk_results and chunk_results.items:
        lines = []
        for item in chunk_results.items:
            metadata = item.metadata
            lines.append(_format_evidence({
                "chunk_id": metadata.get("chunk_id"),
                "file_path": metadata.get("file_path"),
                "excerpt": item.content,
                "score": metadata.get("score"),
            }))
        sections.append("## Direct Codebase Evidence\n" + "\n\n".join(lines))

    if entity_evidence:
        lines = []
        for entity_id, evidence_items in entity_evidence.items():
            if not evidence_items:
                continue
            lines.append(f"### Evidence for entity {entity_id}")
            lines.extend(_format_evidence(item, "- ") for item in evidence_items)
        if lines:
            sections.append("## Entity Evidence\n" + "\n".join(lines))

    if relationship_evidence:
        lines = []
        for key, evidence_items in relationship_evidence.items():
            if not evidence_items:
                continue
            lines.append(f"### Evidence for relationship {key}")
            lines.extend(_format_evidence(item, "- ") for item in evidence_items)
        if lines:
            sections.append("## Relationship Evidence\n" + "\n".join(lines))

    return "\n\n".join(sections)
