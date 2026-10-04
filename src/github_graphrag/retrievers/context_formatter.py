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
            + _format_entity(
                {
                    "canonical_id": record.get("source_id"),
                    "label": record.get("source_label"),
                    "name": record.get("source"),
                }
            )
            + f" -[{record['relationship']}]-> "
            + _format_entity(
                {
                    "canonical_id": record.get("target_id"),
                    "label": record.get("target_label"),
                    "name": record.get("target"),
                }
            )
        )
    return "\n".join(lines)

def format_retrieval_context(
    entity_results,
    community_results,
    communities,
    graph_records,
):
    sections = []

    # ---------------------------------------
    # Retrieved entities
    # ---------------------------------------
    if entity_results.items:
        lines = []

        for item in entity_results.items:
            score = item.metadata.get("score")

            lines.append(
                f"- {_format_entity(item.metadata)} "
                f"(similarity={score:.4f})"
            )

        sections.append(
            "## Relevant Entities\n"
            + "\n".join(lines)
        )

    # ---------------------------------------
    # Retrieved communities
    # ---------------------------------------
    if community_results.items:
        lines = []

        for item in community_results.items:
            community_id = item.metadata["community_id"]
            score = item.metadata.get("score")

            lines.append(
                f"### Community {community_id} "
                f"(similarity={score:.4f})\n"
                f"{item.content}"
            )

        sections.append(
            "## Relevant Communities\n"
            + "\n\n".join(lines)
        )

    # ---------------------------------------
    # Community details
    # ---------------------------------------
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
                + _format_entity(
                    {
                        "canonical_id": relationship["source_id"],
                        "label": relationship["source_label"],
                        "name": relationship["source_name"],
                    }
                )
                + f" -[{relationship['relationship']}]-> "
                + _format_entity(
                    {
                        "canonical_id": relationship["target_id"],
                        "label": relationship["target_label"],
                        "name": relationship["target_name"],
                    }
                )
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

        sections.append(
            "## Community Details\n" + "\n\n".join(lines)
        )

    # ---------------------------------------
    # Graph relationships
    # ---------------------------------------
    graph_context = format_graph_context(
        graph_records
    )

    if graph_context:
        sections.append(
            "## Graph Relationships\n"
            + graph_context
        )

    return "\n\n".join(sections)
