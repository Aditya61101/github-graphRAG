import type {
  EvaluationResult,
  EvidenceSnapshot,
  QuoteRef,
  SourceLocation,
} from "@/types/evaluation.generated";

const labels: Record<string, string> = {
  ADR_DOCUMENT: "Original ADR document",
  INDEXED_CODE: "Indexed code",
  INTRODUCED: "Introduced",
  WORSENED: "Worsened",
  CONFLICTS_FOUND: "Architectural conflicts detected",
  REVIEW_REQUIRED: "Further architectural review required",
  NO_CONFLICTS_FOUND: "No conflicts found in assessed scope",
  NOT_EVALUATED: "Not evaluated",
  CURRENT_OBSERVED: "Matches observed revision",
  STALE: "Stale analysis",
  UNKNOWN: "Freshness unknown",
  COMPLETE_FOR_RETAINED_SCOPE: "Complete for retained scope",
  PARTIAL: "Partial",
  UNASSESSABLE: "Unable to assess",
  SUPPORTED_CONFLICT: "Confirmed conflict",
  NEEDS_REVIEW: "Review required",
  REFUTED: "Refuted",
  PRE_EXISTING: "Pre-existing",
  ADVISORY: "Advisory",
  SUPPORTED: "Supported",
  INSUFFICIENT: "Insufficient evidence",
  MERGE_BASE: "Merge base",
  TARGET_BASE: "Target base",
  PR_HEAD: "PR head",
  SATISFIES: "Satisfies",
  VIOLATES: "Violates",
  NOT_APPLICABLE: "Not applicable",
  PUBLISHED: "Published",
  PENDING: "Pending",
  RETRY: "Retry pending",
  DISABLED: "Disabled",
  FAILED: "Failed",
  SKIPPED: "Skipped",
  COMPLETED: "Completed",
  DEFERRED: "Deferred",
  EVALUATED: "Evaluated",
  RUNNING: "Running",
  QUEUED: "Queued",
};
export const label = (value?: string | null) =>
  value
    ? (labels[value] ?? "Status unavailable (see technical details)")
    : "Not available";
export function dateLabel(seconds?: number | null) {
  return seconds == null
    ? "Not available"
    : new Date(seconds * 1000).toLocaleString();
}
export const prPath = (id: string, number: number) =>
  `/projects/${encodeURIComponent(id)}/pull-requests/${number}`;
export function githubRepo(fullName: string) {
  return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(fullName) &&
    !fullName.split("/").some((part) => part === "." || part === "..")
    ? `https://github.com/${fullName}`
    : null;
}
export function checkUrl(fullName: string, id?: string | null) {
  const repo = githubRepo(fullName);
  return repo && id && /^\d+$/.test(id) ? `${repo}/runs/${id}` : null;
}
export function codeUrl(
  fullName: string,
  sha: string | null,
  location: SourceLocation | null
) {
  const repo = githubRepo(fullName);
  if (
    !repo ||
    !sha ||
    !/^[a-f0-9]{40}$/.test(sha) ||
    !location ||
    location.start_line < 1 ||
    location.end_line < location.start_line ||
    location.path
      .split("/")
      .some((part) => !part || part === "." || part === "..") ||
    location.path.includes("\\") ||
    Array.from(location.path).some((character) => character.charCodeAt(0) < 32)
  )
    return null;
  return `${repo}/blob/${sha}/${location.path.split("/").map(encodeURIComponent).join("/")}#L${location.start_line}-L${location.end_line}`;
}
export function citationSource(
  result: EvaluationResult,
  citation: QuoteRef
): EvidenceSnapshot | undefined {
  return result.evidence.find(
    (source) =>
      source.evidence_id === citation.evidence_id &&
      source.excerpt.includes(citation.quote)
  );
}
export function citationUrl(
  fullName: string,
  source: EvidenceSnapshot,
  citation: QuoteRef
) {
  // Only immutable, exact repository provenance is navigable. Uploaded ADRs retain their excerpts.
  if (
    source.placement !== "EXACT" ||
    !source.location ||
    !source.path ||
    source.location.path !== source.path
  )
    return null;
  const offset = citation.start_char ?? source.excerpt.indexOf(citation.quote);
  if (
    citation.start_char == null &&
    source.excerpt.indexOf(citation.quote, offset + 1) !== -1
  )
    return null;
  if (
    offset < 0 ||
    source.excerpt.slice(offset, offset + citation.quote.length) !==
      citation.quote
  )
    return null;
  const start =
    source.location.start_line +
    source.excerpt.slice(0, offset).split("\n").length -
    1;
  const end = start + citation.quote.split("\n").length - 1;
  if (end > source.location.end_line) return null;
  return codeUrl(fullName, source.commit_sha, {
    path: source.path,
    start_line: start,
    end_line: end,
  });
}
export function reviewReason(code?: string | null) {
  if (!code) return "No additional review limitation recorded.";
  if (code.includes("ORIGINAL_SOURCE"))
    return "The independent review could not validate the required original-source evidence.";
  if (/QUOTE|SPAN|CITATION/.test(code))
    return "Required quotations could not be matched reliably to the original evidence.";
  if (/BUDGET|AUTHORIZATION|NOT_EXECUTED/.test(code))
    return "The independent review was not completed within the available authorization or review limits.";
  return "The available evidence did not meet the validation requirements. Inspect technical details for the recorded reason.";
}
export function findingGroups(result: EvaluationResult) {
  return {
    confirmed: result.findings.filter(
      (f) => f.disposition === "SUPPORTED_CONFLICT"
    ),
    review: result.findings.filter((f) => f.disposition === "NEEDS_REVIEW"),
    other: result.findings.filter(
      (f) => !["SUPPORTED_CONFLICT", "NEEDS_REVIEW"].includes(f.disposition)
    ),
  };
}
