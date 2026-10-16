"""Concise GitHub presentation of canonical results; never rewrites evidence."""

import html
import ipaddress
import re
from urllib.parse import quote, unquote, urlparse

SUMMARY_BYTES = 24000
MAX_CONFIRMED = 6
MAX_REVIEW = 3


def prose(value, limit=240):
    """Escape model/source prose as literal Markdown, including auto-links."""
    value = (
        str(value)
        .replace("`", "")
        .replace("PR_HEAD", "This PR")
        .replace("TARGET_BASE", "the base revision")
        .replace("MERGE_BASE", "the merge base")
    )
    value = re.sub(
        r"\b[0-9a-fA-F]{32,}\b|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b",
        "internal reference",
        value,
    )
    value = re.sub(
        r"(?i)\b(?:https?|ftp|file|data|javascript):\S+|\bwww\.\S+",
        "unverified link omitted",
        value,
    )
    value = " ".join(
        re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", " ", value).split()
    )
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    value = html.escape(value, quote=False).replace("@", "＠")
    # Colon and slash escaping also prevents bare URL auto-linking. Only the
    # renderer's provenance-bound URLs may become clickable links.
    return re.sub(r"([\\`*_{}\[\]()#+.!|~:/-])", r"\\\1", value)


def valid_repository(full_name):
    return bool(
        re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", full_name)
    ) and full_name.split("/")[1] not in {".", ".."}


def code_link(full_name, sha, location):
    if (
        not location
        or not valid_repository(full_name)
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
    ):
        return None
    path = location.path
    if (
        not path
        or len(path) > 1000
        or path.startswith(("/", "\\"))
        or "\\" in path
        or re.search(r"[\x00-\x1f\x7f]", path)
        or any(p in {"", ".", ".."} for p in path.split("/"))
        or type(location.start_line) is not int
        or type(location.end_line) is not int
        or not 0 < location.start_line <= location.end_line
    ):
        return None
    return f"https://github.com/{full_name}/blob/{sha}/{quote(path, safe='/')}#L{location.start_line}-L{location.end_line}"


def details_url(base, repository_id, pr_number):
    if not base:
        return None
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower().rstrip(".")
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "%" in parsed.netloc
        or "\\" in base
        or re.search(r"[\x00-\x20\x7f]", base)
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or "\\" in decoded_path
        or re.search(r"[\x00-\x20\x7f]", decoded_path)
        or "/projects/" in decoded_path
        or any(p in {".", ".."} for p in decoded_path.split("/"))
    ):
        raise ValueError(
            "Check details base must be an accessible public HTTPS deployment base"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject alternative numeric spellings of loopback/private addresses.
        if re.fullmatch(r"[0-9.]+|0x[0-9a-f]+", host) or "." not in host:
            raise ValueError(
                "Numeric frontend hosts require a public IP address"
            ) from None
    else:
        if not address.is_global:
            raise ValueError(
                "Check details URL must not use loopback or private addresses"
            )
    if type(pr_number) is not int or pr_number <= 0:
        raise ValueError("Check details require a valid PR identity")
    return f"{base.rstrip('/')}/projects/{quote(repository_id, safe='')}/pull-requests/{pr_number}"


def _rule(finding):
    return (
        finding.candidate.rule.statement
        if finding.candidate
        else "The architectural requirement cited in the detailed evaluation."
    )


def _source_notes(result, finding, full_name):
    evidence = {e.evidence_id: e for e in result.evidence}
    notes = []
    # A head location must correspond to a literal, revision-bound citation;
    # no location, branch name or line range is invented for presentation.
    head = finding.head_location
    if (
        head
        and any(
            (e := evidence[c.evidence_id]).repository_id
            == result.revision.repository_id
            and e.origin == "PR_HEAD"
            and e.commit_sha == result.revision.head_sha
            and c.location
            and c.location.path == head.path
            and c.location.start_line
            <= head.start_line
            <= head.end_line
            <= c.location.end_line
            for c in finding.citations
        )
        and (link := code_link(full_name, result.revision.head_sha, head))
    ):
        notes.append(f"[View changed code]({link})")
    adrs = {}
    for citation in finding.citations:
        e = evidence[citation.evidence_id]
        # IDs are used only for exact provenance deduplication, never shown.
        if (
            e.kind == "ADR"
            and e.repository_id == result.revision.repository_id
            and (e.adr_id not in adrs or e.location)
        ):
            adrs[e.adr_id] = e
    for e in list(adrs.values())[:2]:
        name = re.sub(
            r"\.(md|markdown|txt|pdf|docx)$",
            "",
            e.source_name or "Governing architectural document",
            flags=re.IGNORECASE,
        )
        name = name.replace("-", " ").replace("_", " ")
        label = prose(name, 110)
        # Manual uploads have no GitHub location. A source_url alone does not
        # establish its repository, revision or precise source position.
        link = (
            code_link(full_name, e.commit_sha or "", e.location)
            if e.placement == "EXACT"
            else None
        )
        notes.append(
            f"[View governing ADR]({link}) — {label}"
            if link
            else f"ADR: {label} (uploaded source; no public link)"
        )
    return " · ".join(notes)


def review_reason(code):
    if code == "WITNESS_ORIGINAL_SOURCE_NOT_REVIEWED":
        return "The independent review could not validate the required original-source evidence."
    if code and ("BUDGET" in code or "PACKET_TOO_LARGE" in code):
        return "The independent review could not finish within the configured assessment limits."
    if code == "AUTHORIZATION_DENIED":
        return "The independent review could not run with the available authorization."
    return "The allegation remains unresolved by the independent review and needs attention."


def render_review(result, full_name):
    """No issue grouping: distinct validated findings retain separate rule entries."""
    if result is None or result.outcome == "NOT_EVALUATED":
        return {
            "title": "Architectural evaluation unavailable",
            "summary": "## Architectural review\n\n**Verdict:** Evaluation unavailable.\n\nThe evaluation could not be performed reliably. No architectural safety conclusion is available.",
        }
    confirmed = [f for f in result.findings if f.disposition == "SUPPORTED_CONFLICT"]
    review = [
        f for f in result.findings if f.disposition in {"NEEDS_REVIEW", "ADVISORY"}
    ]
    c = result.coverage
    needs_attention = bool(
        review
        or result.rejected_findings
        or result.limitations
        or c.uncertain
        or c.omitted
        or c.errors
        or c.scope != "COMPLETE_FOR_RETAINED_SCOPE"
        or result.execution != "COMPLETED"
        or result.outcome == "REVIEW_REQUIRED"
    )
    verdict = (
        "Architectural conflicts detected."
        if confirmed
        else "Architectural review required."
        if needs_attention
        else "No confirmed conflict in the assessed scope."
    )
    sections = ["## Architectural review\n\n**Verdict:** " + verdict]
    if confirmed:
        sections.append(prose(confirmed[0].claim, 420))
        if confirmed[0].suggested_action:
            sections.append(
                "**Suggested action:** " + prose(confirmed[0].suggested_action, 240)
            )
    sections.append(
        "### Review summary\n\n"
        f"- **Supported governing-rule findings:** {len(confirmed)}\n"
        f"- **Additional findings requiring review:** {len(review)}\n"
        f"- **Uncertain / unassessed units:** {c.uncertain} / {c.omitted + c.errors}\n"
        f"- **Coverage:** {'Complete for the retained scope' if c.scope == 'COMPLETE_FOR_RETAINED_SCOPE' else 'Partial' if c.scope == 'PARTIAL' else 'Unassessable'}\n\n"
        "Supported governing-rule findings are not necessarily distinct bugs."
    )
    if confirmed:
        sections.append("### Confirmed architectural conflicts")
        units = {u.unit_id: u for u in result.plan}
        shared_deltas = set.intersection(
            *(set(units[f.unit_id].delta_ids) for f in confirmed)
        )
        if len(confirmed) > 1 and shared_deltas:
            sections.append(
                "These findings relate to the same recorded code change; each governing rule is listed separately."
            )
    shown = 0
    for finding in confirmed[:MAX_CONFIRMED]:
        block = f"**{prose(_rule(finding), 320)}**\n\n{prose(finding.claim, 230)}"
        if notes := _source_notes(result, finding, full_name):
            block += "\n\n" + notes
        if (
            sum(len(s.encode()) + 2 for s in sections) + len(block.encode())
            > SUMMARY_BYTES - 5000
        ):
            break
        sections.append(block)
        shown += 1
    if shown < len(confirmed):
        sections.append(
            f"Showing {shown} of {len(confirmed)} supported governing-rule findings. The complete evaluation retains every finding and its evidence."
        )
    if review or result.rejected_findings:
        sections.append("### Additional review required")
        for finding in review[:MAX_REVIEW]:
            sections.append(
                f"- {prose(finding.claim, 200)} {review_reason(finding.review_error)}"
            )
        if len(review) > MAX_REVIEW:
            sections.append(
                f"{len(review) - MAX_REVIEW} additional review findings are retained in the detailed evaluation."
            )
        if result.rejected_findings:
            quote_failures = sum(
                f.reason == "QUOTE_NOT_FOUND" for f in result.rejected_findings
            )
            reason = (
                " Required quotations could not be matched to the original evidence."
                if quote_failures
                else " Their evidence or independent-review requirements were not satisfied."
            )
            sections.append(
                f"{len(result.rejected_findings)} rejected proof records were retained for audit; they are not confirmed conflicts."
                + reason
            )
    limitations = []
    discovery_codes = {
        "CONSTRAINT_LIMIT",
        "DECISION_LIMIT",
        "CONTEXT_TOKEN_LIMIT",
        "RELATIONSHIP_SEED_OMITTED",
        "SCOPE_CONTEXT_TRUNCATED",
    }
    if any(l.code in discovery_codes for l in result.limitations):
        limitations.append(
            "Architectural discovery was bounded; some requirements or context were outside the retained scope."
        )
    elif result.limitations or c.scope != "COMPLETE_FOR_RETAINED_SCOPE":
        limitations.append(
            "Discovery or assessment coverage is limited; the detailed evaluation explains the remaining scope."
        )
    if c.uncertain or c.omitted or c.errors:
        limitations.append(
            "Some architectural questions remain unresolved"
            + (" even though confirmed conflicts exist." if confirmed else ".")
        )
    sections.append(
        "### Coverage and limitations\n\n"
        f"{c.assessed} assessed · {c.uncertain} uncertain · {c.omitted} omitted · {c.errors} execution errors.\n\n"
        + (
            " ".join(limitations)
            or "No unresolved coverage limitations were recorded for the retained scope."
        )
        + "\n\nThe detailed DecisionGuard evaluation retains original citations, revision comparisons, independent reviews and evaluation history."
    )
    summary = "\n\n".join(sections)
    assert (
        len(summary.encode()) <= SUMMARY_BYTES
    )  # complete Markdown blocks, never mid-link truncation
    return {"title": verdict.rstrip("."), "summary": summary}
