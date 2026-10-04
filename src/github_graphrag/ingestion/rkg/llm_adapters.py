from __future__ import annotations

from typing import Sequence

from openai import AsyncAzureOpenAI
from pydantic import BaseModel, ConfigDict, Field
import warnings

from .interfaces import LocalExtractor, RelationshipValidator
from .models import (
    CandidateKnowledge,
    EvidenceChunk,
    ExtractedEntity,
    ExtractedRelationship,
    RelationshipCandidate,
    ValidatedRelationship,
)
from .prompts import (
    CROSS_CHUNK_SYSTEM_PROMPT,
    CROSS_CHUNK_USER_PROMPT,
    LOCAL_EXTRACTION_SYSTEM_PROMPT,
    LOCAL_EXTRACTION_USER_PROMPT,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PropertyOutput(StrictModel):
    key: str
    value: str | int | float | bool | list[str | int | float | bool]


class ExtractionEntityOutput(StrictModel):
    label: str
    name: str
    properties: list[PropertyOutput]
    source_chunk_refs: list[str]


class ExtractionRelationshipOutput(StrictModel):
    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str
    properties: list[PropertyOutput]
    source_chunk_refs: list[str]


class ExtractionOutput(StrictModel):
    entities: list[ExtractionEntityOutput]
    relationships: list[ExtractionRelationshipOutput]


class RelationshipValidationOutput(StrictModel):
    supported: bool
    confidence: float = Field(ge=0, le=1)
    rationale: str


def _properties_to_dict(
    properties: Sequence[PropertyOutput],
) -> dict[str, str | int | float | bool | list[str | int | float | bool]]:
    result = {}

    for item in properties:
        key = item.key.strip()

        if not key:
            continue

        value = item.value

        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue

        result[key] = value

    return result


def _format_chunks(chunks: Sequence[EvidenceChunk]) -> tuple[str, dict[str, str]]:
    """Expose short stable references to the LLM instead of 64-char hashes."""
    refs: dict[str, str] = {}
    blocks: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        ref = f"C{index}"
        refs[ref] = chunk.chunk_id
        blocks.append(
            f"===== EVIDENCE CHUNK {ref} =====\n"
            f"file: {chunk.file_path}\n"
            f"strategy: {chunk.strategy.value}\n"
            f"\n{chunk.text}"
        )
    return "\n\n".join(blocks), refs


class AzureOpenAILLM:
    """Small Azure OpenAI client wrapper."""

    def __init__(self, client: AsyncAzureOpenAI, deployment: str):
        self.deployment = deployment
        self.client = client

    async def ainvoke(self, prompt: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Azure OpenAI returned an empty response.")
        return content


class AzureOpenAIExtractor(LocalExtractor):
    """Extract candidate architectural knowledge directly from raw chunks."""

    def __init__(self, llm: AzureOpenAILLM):
        self.llm = llm

    async def extract(
        self,
        chunks: Sequence[EvidenceChunk],
        *,
        examples: str = "",
    ) -> list[CandidateKnowledge]:
        if not chunks:
            return []

        formatted_chunks, ref_to_chunk_id = _format_chunks(chunks)
        user_prompt = LOCAL_EXTRACTION_USER_PROMPT.format(chunks=formatted_chunks)
        if examples:
            user_prompt = (
                "FEW-SHOT EXAMPLES (guidance only; do not copy unsupported facts):\n"
                + examples
                + "\n\n"
                + user_prompt
            )

        response = await self.llm.client.beta.chat.completions.parse(
            model=self.llm.deployment,
            messages=[
                {"role": "system", "content": LOCAL_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=ExtractionOutput,
            temperature=0,
        )

        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("Azure OpenAI returned no structured extraction output.")

        by_chunk: dict[str, CandidateKnowledge] = {
            chunk.chunk_id: CandidateKnowledge(chunk_id=chunk.chunk_id)
            for chunk in chunks
        }

        def resolve_refs(refs: Sequence[str]) -> list[str]:
            resolved: list[str] = []
            unknown: list[str] = []
            for raw_ref in refs:
                ref = raw_ref.strip().upper()
                chunk_id = ref_to_chunk_id.get(ref)
                if chunk_id:
                    resolved.append(chunk_id)
                elif ref:
                    unknown.append(ref)
            if unknown:
                warnings.warn(
                    f"Extractor returned unknown evidence chunk refs: {unknown}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            return list(dict.fromkeys(resolved))

        for item in parsed.entities:
            source_ids = resolve_refs(item.source_chunk_refs)
            if not source_ids or not item.name.strip() or not item.label.strip():
                continue
            entity = ExtractedEntity(
                label=item.label.strip(),
                name=item.name.strip(),
                properties=_properties_to_dict(item.properties),
                source_chunk_ids=source_ids,
            )
            for chunk_id in source_ids:
                by_chunk[chunk_id].entities.append(entity.model_copy(deep=True))

        for item in parsed.relationships:
            source_ids = resolve_refs(item.source_chunk_refs)
            if not source_ids:
                continue
            relationship = ExtractedRelationship(
                source_label=item.source_label.strip(),
                source_name=item.source_name.strip(),
                relationship_type=item.relationship_type.strip().upper(),
                target_label=item.target_label.strip(),
                target_name=item.target_name.strip(),
                properties=_properties_to_dict(item.properties),
                source_chunk_ids=source_ids,
            )
            for chunk_id in source_ids:
                by_chunk[chunk_id].relationships.append(
                    relationship.model_copy(deep=True)
                )

        return list(by_chunk.values())


class AzureOpenAIRelationshipValidator(RelationshipValidator):
    def __init__(self, llm: AzureOpenAILLM, minimum_confidence: float = 0.70):
        self.llm = llm
        self.minimum_confidence = minimum_confidence

    async def validate(
        self,
        candidate: RelationshipCandidate,
        *,
        evidence: Sequence[EvidenceChunk],
        source_entity,
        target_entity,
        neighborhood: str,
    ) -> ValidatedRelationship | None:
        evidence_text = "\n\n".join(
            f"===== {chunk.file_path} =====\n{chunk.text}" for chunk in evidence
        ) or "(no evidence available)"

        prompt = CROSS_CHUNK_USER_PROMPT.format(
            source_id=candidate.source_id,
            target_id=candidate.target_id,
            source_label=source_entity.label,
            source_name=source_entity.name,
            source_aliases=source_entity.aliases,
            target_label=target_entity.label,
            target_name=target_entity.name,
            target_aliases=target_entity.aliases,
            relationship_type=candidate.relationship_type,
            reason=candidate.reason,
            evidence=evidence_text,
            neighborhood=neighborhood or "(no existing graph neighborhood)",
        )

        response = await self.llm.client.beta.chat.completions.parse(
            model=self.llm.deployment,
            messages=[
                {"role": "system", "content": CROSS_CHUNK_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format=RelationshipValidationOutput,
            temperature=0,
        )

        parsed = response.choices[0].message.parsed
        if parsed is None:
            print(
                "REJECT: parsed=None",
                candidate.source_id,
                candidate.relationship_type,
                candidate.target_id,
            )
            return None

        if not parsed.supported:
            print(
                "REJECT: unsupported",
                candidate.relationship_type,
                parsed.confidence,
                parsed.rationale,
            )
            return None

        if parsed.confidence < self.minimum_confidence:
            print(
                "REJECT: low-confidence",
                candidate.relationship_type,
                parsed.confidence,
                parsed.rationale,
            )
            return None

        return ValidatedRelationship(
            source_id=candidate.source_id,
            target_id=candidate.target_id,
            relationship_type=candidate.relationship_type,
            properties=dict(candidate.properties),
            confidence=parsed.confidence,
            evidence_chunk_ids=candidate.evidence_chunk_ids,
            rationale=parsed.rationale,
        )

