from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass

from ai_services.ingestion.pr.models import PullRequestChangeSet

from .models import (
    ArchitecturalDelta,
    EvidenceSnapshot,
    FileCoverage,
    Limitation,
    SourceLocation,
    digest,
    stable_id,
)
from .ports import RevisionSourceReader, SourceBlob
from .settings import ContextSettings
from .source import validate_path


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    before: str
    after: str


def parse_hunks(patch: str) -> tuple[Hunk, ...]:
    """Parse unified text patches, validating declared counts (including empty sides)."""
    header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    output = []
    current = None
    old, new = [], []

    def finish():
        if current is None:
            return
        a, ac, b, bc = current
        if len(old) != ac or len(new) != bc:
            raise ValueError("Patch hunk counts do not match payload")
        output.append(Hunk(a, ac, b, bc, "\n".join(old), "\n".join(new)))

    for line in patch.splitlines():
        match = header.match(line)
        if match:
            finish()
            current = (
                int(match[1]),
                int(match[2] or 1),
                int(match[3]),
                int(match[4] or 1),
            )
            old, new = [], []
        elif line.startswith("@@"):
            raise ValueError("Unsupported patch format")
        elif current is not None:
            if line.startswith("\\ No newline"):
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise ValueError("Unexpected patch hunk line")
            if line[0] in {" ", "-"}:
                old.append(line[1:])
            if line[0] in {" ", "+"}:
                new.append(line[1:])
    finish()
    if (
        patch.strip()
        and not output
        and not any(
            line.startswith(
                ("diff --git", "index ", "old mode", "new mode", "similarity index")
            )
            for line in patch.splitlines()
        )
    ):
        raise ValueError("Unrecognized patch")
    return tuple(output)


def text_hunks(before: str, after: str) -> tuple[Hunk, ...]:
    old, new = before.splitlines(), after.splitlines()
    return tuple(
        Hunk(
            a + 1 if b > a else a,
            b - a,
            c + 1 if d > c else c,
            d - c,
            "\n".join(old[a:b]),
            "\n".join(new[c:d]),
        )
        for tag, a, b, c, d in difflib.SequenceMatcher(
            None, old, new, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    )


def symbols(text: str | None, start: int, count: int) -> tuple[str, ...]:
    if text is None or count == 0:
        return ()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min(
                [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())]
            )
            if first <= start + count - 1 and (node.end_lineno or node.lineno) >= start:
                names.add(node.name)
                for decorator in getattr(node, "decorator_list", ()):
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr
                        in {"get", "post", "put", "patch", "delete"}
                        and decorator.args
                        and isinstance(decorator.args[0], ast.Constant)
                        and isinstance(decorator.args[0].value, str)
                    ):
                        names.add(
                            decorator.func.attr.upper() + " " + decorator.args[0].value
                        )
    return tuple(sorted(names))


def classify(path: str, before: str, after: str) -> str:
    text = before + "\n" + after
    if re.search(
        r"@\w+(?:\.\w+)*\.(?:get|post|put|patch|delete)\s*\(|(?:paths:|openapi:)", text
    ):
        return "API"
    if re.search(
        r"\b(?:Column|ForeignKey|create_table|alter_column|CREATE TABLE|ALTER TABLE)\b",
        text,
    ):
        return "SCHEMA"
    if re.search(
        r"\b(?:session|db)\.(?:query|execute|add|commit|delete)\s*\(|\b(?:SELECT|INSERT INTO|UPDATE)\b",
        text,
    ):
        return "DATA_ACCESS"
    if re.search(
        r"\b(?:add_edge|add_conditional_edges|processing_status|set_entry_point)\b",
        text,
    ):
        return "WORKFLOW"
    if path.rsplit("/", 1)[-1] in {
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
    } or re.search(r"^\s*(?:from\s+\S+\s+import|import\s+\S+)", text, re.MULTILINE):
        return "DEPENDENCY"
    if path.endswith((".yaml", ".yml", ".toml", ".ini", ".env", ".json")) or re.search(
        r"\b(?:getenv|environ)\b", text
    ):
        return "CONFIGURATION"
    if re.search(r"^\s*(?:class|(?:async )?def)\s+", text, re.MULTILINE):
        return "COMPONENT"
    return "OTHER"


def classify_scoped(
    path: str, hunk: Hunk, before: str | None, after: str | None
) -> str:
    kind = classify(path, hunk.before, hunk.after)
    # A changed return/body inside a declared route is an API delta even when
    # the decorator itself lies outside the changed hunk.
    scoped = symbols(before, hunk.old_start, hunk.old_count) + symbols(
        after, hunk.new_start, hunk.new_count
    )
    if any(re.match(r"^(GET|POST|PUT|PATCH|DELETE) /", name) for name in scoped):
        return "API"
    return kind


def clipped(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    prefix = text[:limit]
    # Whole lines retain honest line spans; a single long line remains unlocated.
    return prefix[: prefix.rfind("\n")] if "\n" in prefix else prefix


def code_evidence(
    repo: str,
    origin: str,
    path: str,
    commit: str,
    text: str | None,
    settings: ContextSettings,
    *,
    start: int | None = None,
    chunk_id: str | None = None,
    placement: str = "UNLOCATED",
    content_hash: str | None = None,
) -> EvidenceSnapshot:
    excerpt = clipped(text or "", settings.max_evidence_chars)
    location = None
    # Mid-line truncation cannot claim an exact source range.
    if (
        start
        and text
        and placement == "EXACT"
        and (len(excerpt) == len(text) or "\n" in text[: settings.max_evidence_chars])
    ):
        location = SourceLocation(
            path=path, start_line=start, end_line=start + len(excerpt.splitlines()) - 1
        )
    return EvidenceSnapshot(
        evidence_id=stable_id(
            repo, origin, path, commit, chunk_id or "", str(start), digest(excerpt)
        ),
        repository_id=repo,
        kind="CODE",
        origin=origin,
        path=path,
        commit_sha=commit,
        chunk_id=chunk_id,
        excerpt=excerpt,
        excerpt_hash=digest(excerpt),
        content_hash=content_hash or (digest(text) if text is not None else None),
        completeness="UNAVAILABLE"
        if text is None
        else "EXCERPT"
        if len(excerpt) < len(text)
        else "COMPLETE",
        placement=placement,
        location=location,
    )


@dataclass
class DeltaAnalysis:
    deltas: list[ArchitecturalDelta]
    evidence: dict[str, EvidenceSnapshot]
    coverage: list[FileCoverage]
    limitations: list[Limitation]


class CachedSource:
    def __init__(self, source: RevisionSourceReader, repository_id: str):
        self.source, self.repository_id = source, repository_id
        self.cache: dict[tuple[str, str], SourceBlob] = {}

    def read(self, sha: str, path: str) -> SourceBlob:
        key = sha, path
        if key not in self.cache:
            blob = self.source.read_blob(self.repository_id, sha, path)
            if (blob.repository_id, blob.commit_sha, blob.path) != (
                self.repository_id,
                sha,
                path,
            ):
                raise ValueError("Source response escaped requested revision scope")
            if (blob.status == "complete") != (blob.text is not None):
                raise ValueError("Source response text/status mismatch")
            self.cache[key] = blob
        return self.cache[key]


def analyze_delta(
    change_set: PullRequestChangeSet, source: CachedSource, settings: ContextSettings
) -> DeltaAnalysis:
    result = DeltaAnalysis([], {}, [], [])
    for file in sorted(change_set.files, key=lambda f: f.path):
        for text, status in (
            (file.base_content, file.base_content_status),
            (file.head_content, file.head_content_status),
        ):
            if (status == "complete") != (text is not None):
                raise ValueError("Change-set source text/status mismatch")
        validate_path(file.path)
        old_path = validate_path(file.previous_path or file.path)
        reasons = []
        old = (
            file.base_content
            if file.base_content_status == "complete"
            else ""
            if file.base_content_status == "absent"
            else None
        )
        head = (
            file.head_content
            if file.head_content_status == "complete"
            else ""
            if file.head_content_status == "absent"
            else None
        )
        if old is None or head is None:
            reasons.append("CONTENT_UNAVAILABLE")
        try:
            target = source.read(change_set.base_sha, old_path)
            target_text = (
                target.text
                if target.status == "complete"
                else ""
                if target.status == "absent"
                else None
            )
        except Exception as exc:  # noqa: BLE001 - source port failures become sanitized coverage diagnostics
            target_text = None
            reasons.append("TARGET_SOURCE_UNAVAILABLE")
            result.limitations.append(
                Limitation(
                    code="TARGET_SOURCE_UNAVAILABLE",
                    message=type(exc).__name__,
                    paths=(file.path,),
                )
            )
        comparison = (
            "UNAVAILABLE"
            if target_text is None or old is None
            else "UNCHANGED_BASE"
            if target_text == old
            else "DIVERGED"
        )
        if comparison != "UNCHANGED_BASE":
            reasons.append("TARGET_" + comparison)
        hunks = ()
        if file.patch is not None and file.patch_status == "complete":
            try:
                hunks = parse_hunks(file.patch)
                # Validate available patch evidence against the exact ingested side.
                for h in hunks:
                    for text, start, count, snippet in (
                        (old, h.old_start, h.old_count, h.before),
                        (head, h.new_start, h.new_count, h.after),
                    ):
                        if (
                            text is not None
                            and count
                            and "\n".join(
                                text.splitlines()[start - 1 : start - 1 + count]
                            )
                            != snippet
                        ):
                            raise ValueError("Patch disagrees with source contents")
            except ValueError:
                hunks = ()
                reasons.append("INVALID_PATCH")
        elif file.patch_status != "complete":
            reasons.append("PATCH_" + file.patch_status.upper())
        elif file.patch is None and (old is None or head is None):
            reasons.append("PATCH_UNAVAILABLE")
        # Prefer exact content comparison for affected-line precision; patches still
        # provide usable hunks when one side is unavailable, with no trusted location.
        if old is not None and head is not None:
            hunks = text_hunks(old, head)
        if len(hunks) > settings.max_deltas_per_file:
            hunks = hunks[: settings.max_deltas_per_file]
            reasons.append("DELTA_LIMIT")
        state = (
            "TEXT"
            if hunks and old is not None and head is not None
            else "METADATA_ONLY"
            if not hunks
            else "UNSUPPORTED"
        )
        if not hunks:
            hunks = (
                Hunk(
                    1 if old else 0,
                    len((old or "").splitlines()),
                    1 if head else 0,
                    len((head or "").splitlines()),
                    old or "",
                    head or "",
                ),
            )
        target_e = code_evidence(
            change_set.repository_id,
            "TARGET_BASE",
            old_path,
            change_set.base_sha,
            target_text,
            settings,
            start=1 if target_text else None,
            placement="EXACT" if target_text is not None else "UNLOCATED",
        )
        result.evidence[target_e.evidence_id] = target_e
        for h in hunks:
            old_e = code_evidence(
                change_set.repository_id,
                "MERGE_BASE",
                old_path,
                change_set.merge_base_sha,
                h.before if old is not None or h.old_count else None,
                settings,
                start=h.old_start or None,
                placement="EXACT" if old is not None else "UNLOCATED",
            )
            head_e = code_evidence(
                change_set.repository_id,
                "PR_HEAD",
                file.path,
                change_set.head_sha,
                h.after if head is not None or h.new_count else None,
                settings,
                start=h.new_start or None,
                placement="EXACT" if head is not None else "UNLOCATED",
            )
            for e in (old_e, head_e):
                result.evidence[e.evidence_id] = e
            result.deltas.append(
                ArchitecturalDelta(
                    delta_id=stable_id(
                        change_set.revision_key,
                        file.path,
                        str(h.old_start),
                        str(h.new_start),
                    ),
                    kind=classify_scoped(file.path, h, old, head),
                    path=file.path,
                    previous_path=file.previous_path,
                    change_status=file.status,
                    before=old_e.excerpt if old is not None else None,
                    after=head_e.excerpt if head is not None else None,
                    symbols=tuple(
                        sorted(
                            set(
                                symbols(old, h.old_start, h.old_count)
                                + symbols(head, h.new_start, h.new_count)
                            )
                        )
                    ),
                    old_location=old_e.location,
                    head_location=head_e.location,
                    evidence_ids=(
                        old_e.evidence_id,
                        target_e.evidence_id,
                        head_e.evidence_id,
                    ),
                    target_comparison=comparison,
                    analysis=state,
                )
            )
        for e in (target_e, old_e, head_e):
            if e.completeness == "EXCERPT":
                reasons.append("EVIDENCE_EXCERPT")
        result.coverage.append(
            FileCoverage(
                path=file.path,
                status=state,
                mapped=False,
                reasons=tuple(sorted(set(reasons))),
            )
        )
        for reason in sorted(set(reasons)):
            result.limitations.append(
                Limitation(
                    code=reason,
                    message="PR source coverage limitation",
                    paths=(file.path,),
                )
            )
        if file.path.endswith(".py") and (head is not None or old is not None):
            for text in (old, head):
                if text:
                    try:
                        ast.parse(text)
                    except SyntaxError:
                        result.limitations.append(
                            Limitation(
                                code="SYMBOL_PARSE_UNSUPPORTED",
                                message="Python source did not parse",
                                paths=(file.path,),
                            )
                        )
                        break
        elif file.path.endswith((".js", ".ts", ".tsx", ".java", ".go", ".rs")):
            result.limitations.append(
                Limitation(
                    code="SYMBOL_PARSE_UNSUPPORTED",
                    message="No language-specific symbol mapper; text mapping only",
                    paths=(file.path,),
                )
            )
    return result
