"""Community summarizer synthesizing architectural summaries from community contexts."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Sequence

from .config import CommunityConfig
from .exceptions import CommunitySummaryError
from .interfaces import CommunitySummarizer, SummarizationLLM
from .models import (
    CommunityContext,
    CommunityProcessingState,
    CommunitySummary,
    CommunitySummaryOutput,
)

logger = logging.getLogger(__name__)

COMMUNITY_SUMMARIZER_SYSTEM_PROMPT = """
You are an expert software architecture analyst.

You are analyzing an architectural community extracted from a software repository knowledge graph.
The community contains components and their internal directed relationships.

CRITICAL ARCHITECTURAL RULES:
1. This is a software architecture community. The provided entities and relationships are authoritative facts.
2. Preserve relationship direction exactly as provided (source -[TYPE]-> target).
3. Do NOT invent entities, classes, databases, or components.
4. Do NOT invent relationships or infer relationships that are not explicitly provided.
5. Describe the architectural responsibility or theme of this community (e.g., "User Authentication & Session Management", "Data Ingestion & Event Processing").
6. Prefer concrete component names and explicit interactions over vague generalities.
7. Do NOT simply list every entity. Explain how they interact as a functional group.
8. Do NOT include irrelevant low-level implementation details.
9. If the community is small, single-entity, or ambiguous, describe only what is directly supported by the evidence.
10. All key_entities must be exact names of entities present in the supplied community context.

You MUST respond with a valid JSON object matching this schema:
{
    "architectural_role": "<Concise 3-7 word title of this community's functional role, max 200 chars>",
    "summary": "<2-4 sentence cohesive architectural summary explaining what this group does and how components interact, max 2000 chars>",
    "key_entities": ["<entity1>", "<entity2>"]
}
"""

COMMUNITY_SUMMARIZER_USER_PROMPT = """
Analyze the following architectural community context:

{context}

Return ONLY the JSON object. Do not include introductory text or trailing markdown explanations.
"""


def _extract_json_content(raw_text: str) -> str:
    """Extract JSON string from potential markdown code fences or whitespace."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


class LLMCommunitySummarizer(CommunitySummarizer):
    """Generates structured architectural summaries for communities using an LLM."""

    def __init__(
        self,
        llm: SummarizationLLM,
        config: CommunityConfig | None = None,
    ) -> None:
        self.llm = llm
        self.config = config or CommunityConfig()

    async def summarize(self, context: CommunityContext) -> CommunitySummary:
        """Generate an architectural summary for a single community context."""
        context_text = context.format_text()
        user_prompt = COMMUNITY_SUMMARIZER_USER_PROMPT.format(context=context_text)
        full_prompt = f"{COMMUNITY_SUMMARIZER_SYSTEM_PROMPT}\n\n{user_prompt}"

        try:
            raw_response = await self.llm.ainvoke(full_prompt)
            response_text = (
                raw_response.content
                if hasattr(raw_response, "content")
                else str(raw_response)
            )

            json_text = _extract_json_content(response_text)
            data = json.loads(json_text)
            output = CommunitySummaryOutput.model_validate(data)

            # Validate that key_entities belong to the community
            valid_names = {m.entity_name for m in context.members}
            valid_ids = {m.entity_id for m in context.members}
            validated_keys = [
                k for k in output.key_entities
                if k in valid_names or k in valid_ids
            ]

            # If LLM returned hallucinated keys, fall back to valid community member names
            if not validated_keys:
                validated_keys = [m.entity_name for m in context.members[:5]]

            output.key_entities = validated_keys

            return CommunitySummary.create(
                community_id=context.community_id,
                output=output,
                context_hash=context.context_hash(),
            )
        except Exception as exc:
            raise CommunitySummaryError(
                f"Failed to summarize community: {exc}",
                community_id=context.community_id,
                cause=exc,
            ) from exc

    async def summarize_batch(
        self,
        contexts: Sequence[CommunityContext],
        existing_states: dict[str | int, CommunityProcessingState] | None = None,
        force_refresh: bool = False,
    ) -> tuple[list[CommunitySummary], list[CommunitySummary]]:
        """Process community contexts, generating new summaries or reusing unchanged ones.

        Returns:
            (newly_generated_summaries, reused_existing_summaries)
        """
        states = existing_states or {}
        semaphore = asyncio.Semaphore(max(1, self.config.summary_concurrency))

        new_summaries: list[CommunitySummary] = []
        reused_summaries: list[CommunitySummary] = []

        to_generate: list[CommunityContext] = []

        for ctx in contexts:
            state = states.get(ctx.community_id)
            c_hash = ctx.context_hash()

            if (
                not force_refresh
                and state is not None
                and state.has_summary
                and state.context_hash == c_hash
                and state.summary is not None
                and state.architectural_role is not None
                and state.summary_hash is not None
            ):
                # Context unchanged: reuse existing summary!
                reused_summaries.append(
                    CommunitySummary(
                        community_id=ctx.community_id,
                        summary=state.summary,
                        architectural_role=state.architectural_role,
                        key_entities=list(state.key_entities),
                        summary_hash=state.summary_hash,
                        context_hash=state.context_hash,
                    )
                )
            else:
                to_generate.append(ctx)

        if to_generate:
            async def _process_one(c: CommunityContext) -> CommunitySummary:
                async with semaphore:
                    return await self.summarize(c)

            tasks = [_process_one(c) for c in to_generate]
            new_summaries = list(await asyncio.gather(*tasks))

        return new_summaries, reused_summaries
