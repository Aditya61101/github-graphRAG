from github_graphrag.query_entities import format_graph_context

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
                f"- {item.content} "
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
    # Community membership
    # ---------------------------------------
    if communities:
        lines = []

        for community in communities:
            community_id = community["community_id"]
            entities = community["entities"]

            lines.append(
                f"- Community {community_id}: "
                + ", ".join(entities)
            )

        sections.append(
            "## Community Membership\n"
            + "\n".join(lines)
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