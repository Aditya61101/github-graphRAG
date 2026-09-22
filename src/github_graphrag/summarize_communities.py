async def generate_community_summary(
    llm,
    community_id,
    entities,
    relationships,
):
    context = f"""
        Community ID: {community_id}

        Entities:
        {chr(10).join(f"- {e}" for e in entities)}

        Relationships:
        {chr(10).join(f"- {r}" for r in relationships)}
    """

    prompt = f"""
You are analyzing the architecture of a software repository.

The following components belong to the same graph community.

{context}

Write a concise architectural summary of this community.

Explain:
- what responsibility this group appears to have
- the important components
- how the components interact

Only use information supported by the supplied graph.
Do not invent implementation details.

Return only the summary.
"""

    response = await llm.ainvoke(prompt)

    return response.content