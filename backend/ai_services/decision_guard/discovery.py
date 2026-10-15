"""Step 2A: deterministic discovery views, never policy interpretation/retrieval.

Seeds reference shared immutable original evidence. Query text is a bounded,
untrusted projection of that evidence, not a new citation or an instruction.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from pydantic import model_validator

from .delta import code_evidence, declaration_window
from .models import (
    AffectedEntityCandidate,
    ArchitecturalRelationship,
    BaselineSnapshot,
    Contract,
    EvidenceSnapshot,
    FileCoverage,
    Limitation,
    RevisionIdentity,
    stable_id,
)
from .settings import ContextSettings

Channel = Literal["CODE_LEXICAL", "ADR_LEXICAL", "ADR_SEMANTIC"]


@dataclass(frozen=True)
class QuerySettings:
    max_seeds: int = 32
    max_seeds_per_file: int = 8
    max_queries: int = 96
    max_terms: int = 64
    max_term_chars: int = 96
    max_lexical_chars: int = 1600
    max_semantic_chars: int = 3200
    max_excerpt_chars: int = 800
    max_query_sources: int = 8
    max_entities_per_seed: int = 12
    max_relationships_per_seed: int = 8
    context_lines: int = 4
    max_pr_hint_chars: int = 400

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in self.__dict__.values()):
            raise ValueError("Query bounds must be positive integers")


class ChangeSeed(Contract):
    seed_id: str
    delta_id: str
    path: str
    previous_path: str | None = None
    change_type: Literal[
        "added", "modified", "removed", "renamed", "copied", "type_changed"
    ]
    changed_symbols: tuple[str, ...] = ()  # Merge-base/head union, not head existence.
    identifiers: tuple[str, ...] = ()
    literals: tuple[str, ...] = ()
    mapping: Literal["RESOLVED", "FILE_FALLBACK"]
    baseline_entity_ids: tuple[str, ...] = ()
    new_candidate_ids: tuple[str, ...] = ()
    relationship_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    query_source_ids: tuple[str, ...] = ()


class RetrievalQuery(Contract):
    query_id: str
    channel: Channel
    text: str
    seed_ids: tuple[str, ...]
    source_evidence_ids: tuple[str, ...]
    content_role: Literal["UNTRUSTED_DISCOVERY_DATA"] = "UNTRUSTED_DISCOVERY_DATA"


class PreliminaryDiscovery(Contract):
    schema_version: Literal["dg-discovery-v1"] = "dg-discovery-v1"
    query_settings: QuerySettings = QuerySettings()
    revision: RevisionIdentity
    baseline: BaselineSnapshot
    seeds: tuple[ChangeSeed, ...]
    queries: tuple[RetrievalQuery, ...]
    entities: tuple[AffectedEntityCandidate, ...]
    relationships: tuple[ArchitecturalRelationship, ...]
    evidence: tuple[EvidenceSnapshot, ...]
    files: tuple[FileCoverage, ...]
    limitations: tuple[Limitation, ...]
    available_delta_count: int
    pr_title_hint: str = ""
    pr_body_hint: str = ""
    # PR hints and all query text are untrusted, separate from retrieval instructions.

    @model_validator(mode="after")
    def integrity(self):
        if (
            self.baseline.repository_id != self.revision.repository_id
            or self.baseline.indexed_rkg_sha != self.revision.base_sha
        ):
            raise ValueError("Discovery baseline/repository mismatch")
        evidence = {e.evidence_id: e for e in self.evidence}
        entities = {e.entity_id: e for e in self.entities}
        relationships = {r.relationship_id: r for r in self.relationships}
        seeds = {s.seed_id: s for s in self.seeds}
        if (
            len(evidence) != len(self.evidence)
            or len(entities) != len(self.entities)
            or len(relationships) != len(self.relationships)
            or len(seeds) != len(self.seeds)
        ):
            raise ValueError("Duplicate discovery identity")
        roles = {
            "MERGE_BASE": self.revision.merge_base_sha,
            "TARGET_BASE": self.revision.base_sha,
            "PR_HEAD": self.revision.head_sha,
        }
        for e in self.evidence:
            if (
                e.repository_id != self.revision.repository_id
                or e.origin in roles
                and e.commit_sha != roles[e.origin]
            ):
                raise ValueError("Discovery source escaped repository/revision")
        for entity in self.entities:
            if not set(entity.evidence_ids) <= evidence.keys():
                raise ValueError("Discovery entity missing original evidence")
        for relationship in self.relationships:
            if (
                relationship.provenance != "OBSERVED_CODE"
                or not {relationship.source_id, relationship.target_id}
                <= entities.keys()
                or not set(relationship.evidence_ids) <= evidence.keys()
            ):
                raise ValueError("Discovery relationship missing source group")
        for seed in self.seeds:
            if (
                not set(seed.baseline_entity_ids + seed.new_candidate_ids)
                <= entities.keys()
                or not set(seed.relationship_ids) <= relationships.keys()
                or not set(seed.evidence_ids) <= evidence.keys()
                or not set(seed.query_source_ids) <= set(seed.evidence_ids)
            ):
                raise ValueError("Discovery seed has unresolved references")
        for query in self.queries:
            assigned_sources = {
                eid
                for sid in query.seed_ids
                if sid in seeds
                for eid in seeds[sid].evidence_ids
            }
            if (
                not set(query.seed_ids) <= seeds.keys()
                or not set(query.source_evidence_ids) <= assigned_sources
                or any(
                    evidence[eid].kind != "CODE" for eid in query.source_evidence_ids
                )
            ):
                raise ValueError("Discovery query escaped its source units")
        return self


def identifier_terms(text):
    """Exact spelling plus snake/camel/acronym components, in stable source order."""
    terms = []
    for identifier in re.findall(r"[A-Za-z_$][\w$]*", text):
        terms.append(identifier)
        split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", identifier)
        split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", split)
        terms.extend(p.casefold() for p in re.split(r"[_$\W]+", split) if p)
    return tuple(dict.fromkeys(terms))


def meaningful_literals(text):
    # Quoted source data, not inferred domain terminology or policy meaning.
    return tuple(
        dict.fromkeys(m[1] for m in re.findall(r"""(['"])([^'"\n]{1,96})\1""", text))
    )


def _fair_units(deltas, limits, omit):
    queues = defaultdict(list)
    for delta in sorted(
        deltas,
        key=lambda d: (
            d.path,
            d.old_location.start_line if d.old_location else 0,
            d.delta_id,
        ),
    ):
        queues[delta.path].append(delta)
    for path, rows in queues.items():
        for delta in rows[limits.max_seeds_per_file :]:
            omit(
                "DISCOVERY_PER_FILE_LIMIT",
                "Change unit omitted by per-file seed bound",
                (path,),
                (delta.delta_id,),
            )
        queues[path] = rows[: limits.max_seeds_per_file]
    ordered = [
        rows[index]
        for index in range(limits.max_seeds_per_file)
        for _, rows in sorted(queues.items())
        if index < len(rows)
    ]
    for delta in ordered[limits.max_seeds :]:
        omit(
            "DISCOVERY_SEED_LIMIT",
            "Change unit omitted by fair PR seed bound",
            (delta.path,),
            (delta.delta_id,),
        )
    return ordered[: limits.max_seeds]


def _source_context(changes, delta, limits):
    file = next(f for f in changes.files if f.path == delta.path)
    for origin, sha, path, text, location in (
        (
            "MERGE_BASE",
            changes.merge_base_sha,
            delta.previous_path or delta.path,
            file.base_content,
            delta.old_location,
        ),
        (
            "PR_HEAD",
            changes.head_sha,
            delta.path,
            file.head_content,
            delta.head_location,
        ),
    ):
        if text is None or location is None:
            continue
        rows = text.splitlines()
        enclosing = (
            declaration_window(
                text, location.start_line, location.end_line - location.start_line + 1
            )
            if path.endswith(".py")
            else None
        )
        first, last = enclosing or (
            max(1, location.start_line - limits.context_lines),
            min(len(rows), location.end_line + limits.context_lines),
        )
        excerpt = "\n".join(rows[first - 1 : last])
        if not excerpt:
            continue
        yield code_evidence(
            changes.repository_id,
            origin,
            path,
            sha,
            excerpt,
            ContextSettings(max_evidence_chars=limits.max_excerpt_chars),
            start=first,
            placement="EXACT",
        )


def _bounded_terms(raw, limits):
    unique = tuple(dict.fromkeys(t for t in raw if t.strip()))
    chosen, size = [], 0
    for term in unique:
        # Keep exact short identifiers; do not invent a truncated identifier.
        if len(term) > limits.max_term_chars:
            continue
        if (
            len(chosen) >= limits.max_terms
            or size + len(term) + bool(chosen) > limits.max_lexical_chars
        ):
            continue
        chosen.append(term)
        size += len(term) + (1 if len(chosen) > 1 else 0)
    return " ".join(chosen), len(chosen) < len(unique)


def _query_views(seed, entities, relationships, evidence, limits):
    names = [
        entities[eid].name for eid in seed.baseline_entity_ids + seed.new_candidate_ids
    ]
    source_ids = seed.query_source_ids
    source_text = "\n".join(
        evidence[eid].excerpt[: limits.max_excerpt_chars] for eid in source_ids
    )
    source_literals = [
        literal
        for literal in meaningful_literals(source_text)
        if not any(character.isspace() for character in literal)
    ]
    exact = [*seed.changed_symbols, *names]
    paths = [seed.path, *(p for p in (seed.previous_path,) if p)]
    code_terms = [
        *exact,
        *paths,
        *identifier_terms(" ".join(exact + paths)),
        *seed.identifiers,
        *seed.literals,
        *source_literals,
        *identifier_terms(source_text),
    ]
    adr_terms = [
        *seed.literals,
        *source_literals,
        *exact,
        *identifier_terms(" ".join(exact)),
        *seed.identifiers,
        *identifier_terms(source_text),
        *paths,
        *identifier_terms(" ".join(paths)),
    ]
    for channel, terms in (("CODE_LEXICAL", code_terms), ("ADR_LEXICAL", adr_terms)):
        text, truncated = _bounded_terms(terms, limits)
        yield channel, text, source_ids, truncated
    # A data representation, never an architectural explanation or full PR diff.
    heading = json.dumps(
        {
            "path": seed.path,
            "change_type": seed.change_type,
            "changed_symbols": seed.changed_symbols,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    links = [
        f"{entities[r.source_id].name} {r.relationship_type} {entities[r.target_id].name}"
        for rid in seed.relationship_ids
        for r in (relationships[rid],)
    ]
    blocks = (
        [heading, "Baseline source-backed neighborhood: " + "; ".join(links)]
        if links
        else [heading]
    )
    blocks += [
        f"{evidence[eid].origin} {evidence[eid].path}:\n{evidence[eid].excerpt[: limits.max_excerpt_chars]}"
        for eid in source_ids
    ]
    text = "\n".join(blocks)
    yield (
        "ADR_SEMANTIC",
        text[: limits.max_semantic_chars],
        source_ids,
        len(text) > limits.max_semantic_chars,
    )


def build_discovery(
    changes,
    baseline,
    analysis,
    entities,
    relationships,
    evidence,
    verified_baseline,
    limits=None,
):
    """Project existing preliminary state, before any ADR catalog read or pruning."""
    limits = limits or QuerySettings()
    evidence = dict(evidence)  # Do not mutate original context packing inputs.
    limitations = list(analysis.limitations)

    def omit(code, message, paths=(), ids=()):
        limitations.append(
            Limitation(
                code=code, message=message, paths=tuple(paths), item_ids=tuple(ids)
            )
        )

    scope_id = stable_id(
        changes.repository_id,
        changes.revision_key,
        changes.merge_base_sha,
        str(baseline.graph_generation),
        baseline.ingestion_run_id,
    )
    code_entities = {
        eid: e
        for eid, e in entities.items()
        if e.origin == "CODE" and set(e.evidence_ids) & verified_baseline
    }
    observed = {
        rid: r
        for rid, r in relationships.items()
        if r.provenance == "OBSERVED_CODE"
        and set(r.evidence_ids) & verified_baseline
        and r.source_id in code_entities
        and r.target_id in code_entities
    }
    for rid in sorted(set(relationships) - set(observed)):
        omit(
            "DISCOVERY_RELATIONSHIP_UNSUPPORTED",
            "Non-code or uncorroborated relationship excluded from source-grounded query neighborhood",
            ids=(rid,),
        )
    new_entities = {
        eid: e for eid, e in entities.items() if e.origin == "NEW_CANDIDATE"
    }
    available_entities = code_entities | new_entities
    seeds, used_entities, used_edges, used_evidence = [], set(), set(), set()
    retained_deltas = _fair_units(analysis.deltas, limits, omit)
    for delta in retained_deltas:
        seed_id = stable_id(scope_id, delta.delta_id)
        primary = {
            eid
            for eid, e in code_entities.items()
            if delta.delta_id in e.delta_ids
            and e.mapping in {"CHANGED_SYMBOL", "CHANGED_CHUNK"}
        }
        candidates = primary or {
            eid
            for eid, e in code_entities.items()
            if delta.path in e.paths
            or delta.previous_path
            and delta.previous_path in e.paths
        }
        mapping = "RESOLVED" if primary else "FILE_FALLBACK"
        if not primary:
            omit(
                "DISCOVERY_UNMAPPED_UNIT",
                "File/hunk retrieval seed has no resolved changed baseline symbol",
                (delta.path,),
                (seed_id,),
            )
        edges = sorted(
            (r for r in observed.values() if {r.source_id, r.target_id} & candidates),
            key=lambda r: (
                r.relationship_type != "CALLS",
                r.relationship_type not in {"READS_FROM", "WRITES_TO"},
                r.relationship_id,
            ),
        )
        for edge in edges[limits.max_relationships_per_seed :]:
            omit(
                "DISCOVERY_NEIGHBOR_LIMIT",
                "Direct source-backed edge omitted from seed",
                (delta.path,),
                (edge.relationship_id,),
            )
        edges = edges[: limits.max_relationships_per_seed]
        neighbor_ids = {eid for r in edges for eid in (r.source_id, r.target_id)}
        ids = sorted(
            candidates | neighbor_ids,
            key=lambda eid: (
                eid not in primary,
                eid not in candidates,
                code_entities[eid].distance,
                eid,
            ),
        )
        for eid in ids[limits.max_entities_per_seed :]:
            omit(
                "DISCOVERY_ENTITY_LIMIT",
                "Baseline entity omitted from seed",
                (delta.path,),
                (eid,),
            )
        ids = ids[: limits.max_entities_per_seed]
        kept_edges = [r for r in edges if {r.source_id, r.target_id} <= set(ids)]
        for edge in edges:
            if edge not in kept_edges:
                omit(
                    "DISCOVERY_NEIGHBOR_LIMIT",
                    "Edge omitted with its endpoint group",
                    (delta.path,),
                    (edge.relationship_id,),
                )
        local = tuple(
            sorted(
                eid for eid, e in new_entities.items() if delta.delta_id in e.delta_ids
            )
        )[: limits.max_entities_per_seed]
        context_ids = []
        for value in _source_context(changes, delta, limits):
            if value.evidence_id in evidence and value != evidence[value.evidence_id]:
                # Equal prefix/placement can come from different enclosing spans.
                # Never replace an original snapshot's content hash/completeness.
                value = value.model_copy(
                    update={
                        "evidence_id": stable_id(
                            value.evidence_id,
                            "DISCOVERY_CONTEXT",
                            value.content_hash or "",
                        )
                    }
                )
            evidence[value.evidence_id] = value
            context_ids.append(value.evidence_id)
        original_ids = {
            eid
            for obj in (*[code_entities[eid] for eid in ids], *kept_edges)
            for eid in obj.evidence_ids
            if eid in verified_baseline or evidence[eid].kind == "GRAPH_ASSERTION"
        }
        all_ids = set(delta.evidence_ids) | set(context_ids) | original_ids
        # Head/before precede enclosing context and neighbor declarations/support.
        # Target-base remains distinctly tagged and independently available.
        ordered = [
            eid
            for origin in ("PR_HEAD", "MERGE_BASE")
            for eid in delta.evidence_ids
            if evidence[eid].origin == origin
        ]
        ordered += context_ids
        ordered += sorted(
            original_ids,
            key=lambda eid: (
                not any(
                    eid in code_entities[e].evidence_ids
                    and evidence[eid].path in code_entities[e].paths
                    for e in ids
                    if e not in candidates
                ),
                eid,
            ),
        )
        ordered += [
            eid for eid in delta.evidence_ids if evidence[eid].origin == "TARGET_BASE"
        ]
        sources = tuple(
            dict.fromkeys(
                eid
                for eid in ordered
                if evidence[eid].kind == "CODE"
                and evidence[eid].completeness != "UNAVAILABLE"
            )
        )
        if len(sources) > limits.max_query_sources:
            omit(
                "DISCOVERY_QUERY_SOURCE_LIMIT",
                "Original sources remain in audit but are omitted from query text",
                (delta.path,),
                sources[limits.max_query_sources :],
            )
        sources = sources[: limits.max_query_sources]
        for eid in sources:
            if len(evidence[eid].excerpt) > limits.max_excerpt_chars:
                omit(
                    "DISCOVERY_QUERY_EXCERPT_LIMIT",
                    "Query uses only a bounded prefix of original source evidence",
                    (delta.path,),
                    (eid,),
                )
        text = "\n".join((delta.before or "", delta.after or ""))
        identifiers = identifier_terms(text)
        literals = meaningful_literals(text)
        if (
            len(identifiers) > limits.max_terms
            or len(literals) > limits.max_terms
            or len(delta.symbols) > limits.max_terms
        ):
            omit(
                "DISCOVERY_TERM_LIMIT",
                "Changed-source terms bounded for seed metadata",
                (delta.path,),
                (seed_id,),
            )
        seeds.append(
            ChangeSeed(
                seed_id=seed_id,
                delta_id=delta.delta_id,
                path=delta.path,
                previous_path=delta.previous_path,
                change_type=delta.change_status,
                changed_symbols=delta.symbols[: limits.max_terms],
                identifiers=identifiers[: limits.max_terms],
                literals=literals[: limits.max_terms],
                mapping=mapping,
                baseline_entity_ids=tuple(ids),
                new_candidate_ids=local,
                relationship_ids=tuple(r.relationship_id for r in kept_edges),
                evidence_ids=tuple(sorted(all_ids)),
                query_source_ids=sources,
            )
        )
        used_entities.update(ids + list(local))
        used_edges.update(r.relationship_id for r in kept_edges)
        used_evidence.update(all_ids)
    # Original entity/edge records may carry shared support from another seed.
    # Keep that original evidence once, separate from the bounded query views.
    used_evidence.update(
        eid
        for entity_id in used_entities
        for eid in available_entities[entity_id].evidence_ids
    )
    used_evidence.update(
        eid for rid in used_edges for eid in observed[rid].evidence_ids
    )
    # Same-channel equivalent text merges all originating unit/source associations.
    equivalent = {}
    for channel in ("CODE_LEXICAL", "ADR_LEXICAL", "ADR_SEMANTIC"):
        for seed in seeds:
            view = next(
                v
                for v in _query_views(
                    seed, available_entities, observed, evidence, limits
                )
                if v[0] == channel
            )
            _, text, source_ids, truncated = view
            if not text:
                text = seed.path[: limits.max_lexical_chars]
                truncated = True
            key = channel, text
            if truncated:
                omit(
                    "DISCOVERY_QUERY_TEXT_LIMIT",
                    channel + " query projected within configured bounds",
                    (seed.path,),
                    (seed.seed_id,),
                )
            if key not in equivalent:
                equivalent[key] = RetrievalQuery(
                    query_id=stable_id(scope_id, channel, text),
                    channel=channel,
                    text=text,
                    seed_ids=(seed.seed_id,),
                    source_evidence_ids=tuple(sorted(source_ids)),
                )
            else:
                query = equivalent[key]
                equivalent[key] = query.model_copy(
                    update={
                        "seed_ids": tuple(
                            sorted(set(query.seed_ids + (seed.seed_id,)))
                        ),
                        "source_evidence_ids": tuple(
                            sorted(set(query.source_evidence_ids + tuple(source_ids)))
                        ),
                    }
                )
    queries = list(equivalent.values())
    for query in queries[limits.max_queries :]:
        omit(
            "DISCOVERY_QUERY_LIMIT",
            query.channel + " omitted by PR query count bound",
            ids=(query.query_id, *query.seed_ids),
        )
    retained_paths = {seed.path for seed in seeds}
    files = tuple(
        f.model_copy(
            update={
                "mapped": any(
                    s.path == f.path and s.mapping == "RESOLVED" for s in seeds
                ),
                "reasons": tuple(
                    sorted(
                        set(
                            f.reasons
                            + (
                                ("DISCOVERY_SEED_OMITTED",)
                                if f.path not in retained_paths
                                else ()
                            )
                        )
                    )
                ),
            }
        )
        for f in analysis.coverage
    )
    if (
        len(changes.title) > limits.max_pr_hint_chars
        or len(changes.body or "") > limits.max_pr_hint_chars
    ):
        omit(
            "DISCOVERY_PR_HINT_LIMIT",
            "Untrusted PR metadata bounded separately from source queries",
        )
    return PreliminaryDiscovery(
        query_settings=limits,
        revision=RevisionIdentity(
            repository_id=changes.repository_id,
            github_repository_id=str(changes.github_repository_id),
            pull_request_number=changes.pull_request_number,
            base_sha=changes.base_sha,
            head_sha=changes.head_sha,
            merge_base_sha=changes.merge_base_sha,
            revision_key=changes.revision_key,
        ),
        baseline=baseline,
        seeds=tuple(seeds),
        queries=tuple(queries[: limits.max_queries]),
        entities=tuple(available_entities[eid] for eid in sorted(used_entities)),
        relationships=tuple(observed[rid] for rid in sorted(used_edges)),
        evidence=tuple(evidence[eid] for eid in sorted(used_evidence)),
        files=files,
        limitations=tuple(
            sorted(limitations, key=lambda l: (l.code, l.paths, l.item_ids, l.message))
        ),
        available_delta_count=len(analysis.deltas),
        pr_title_hint=changes.title[: limits.max_pr_hint_chars],
        pr_body_hint=(changes.body or "")[: limits.max_pr_hint_chars],
    )
