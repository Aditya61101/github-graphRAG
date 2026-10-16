import type {
  EvaluationResult,
  EvidenceSnapshot,
  Finding,
  QuoteRef,
} from "@/types/evaluation.generated";
import { citationSource } from "@/lib/review-presentation";

export function readable(text: string, length = 190) {
  const plain = text
    .replaceAll("`", "")
    .replaceAll("PR_HEAD", "PR head")
    .replace(/\s+/g, " ")
    .trim();
  if (plain.length <= length) return plain;
  const end = plain.lastIndexOf(" ", length);
  return plain.slice(0, end > length / 2 ? end : length) + "…";
}
export function acceptedReview(finding: Finding) {
  return (
    finding.disposition === "SUPPORTED_CONFLICT" &&
    !finding.review_error &&
    !finding.review_rejection &&
    finding.review?.verdict === "SUPPORTED"
  );
}
export function acceptedChallenger(finding: Finding) {
  return (
    acceptedReview(finding) ||
    (finding.disposition === "REFUTED" &&
      finding.review?.verdict === "REFUTED" &&
      !finding.review_error &&
      !finding.review_rejection)
  );
}
export function sharedChange(result: EvaluationResult, findings: Finding[]) {
  if (findings.length < 2) return false;
  const changes = findings.map(
    (finding) =>
      result.plan.find((unit) => unit.unit_id === finding.unit_id)?.delta_ids ??
      []
  );
  return changes[0].some((id) => changes.every((ids) => ids.includes(id)));
}
export function adrNames(result: EvaluationResult, finding: Finding) {
  return [
    ...new Set(
      (finding.candidate?.rule.citations ?? [])
        .map((citation) => citationSource(result, citation))
        .filter((source) => source?.kind === "ADR")
        .map((source) => source!.source_name ?? "Architectural document")
    ),
  ];
}
export function findingCitations(finding: Finding): QuoteRef[] {
  return [
    ...finding.citations,
    ...(finding.candidate?.rule.citations ?? []),
    ...(finding.candidate?.revisions.flatMap(
      (revision) => revision.citations
    ) ?? []),
    ...(finding.review?.citations ?? []),
    ...(finding.review?.revisions.flatMap((revision) => revision.citations) ??
      []),
  ];
}
export function evidenceGroups(result: EvaluationResult) {
  const groups = new Map<
    string,
    { citation: QuoteRef; source: EvidenceSnapshot; findingIds: string[] }
  >();
  for (const finding of result.findings)
    for (const citation of findingCitations(finding)) {
      const source = citationSource(result, citation);
      if (!source) continue;
      let start = citation.start_char;
      if (start == null) {
        start = source.excerpt.indexOf(citation.quote);
        // Ambiguous source occurrences retain their original citation separately.
        if (source.excerpt.indexOf(citation.quote, start + 1) !== -1)
          start = null;
      }
      const key = JSON.stringify([
        source.evidence_id,
        start ?? finding.finding_id,
        citation.end_char ??
          (start == null ? null : start + citation.quote.length),
        citation.quote,
      ]);
      const group = groups.get(key) ?? { citation, source, findingIds: [] };
      if (!group.findingIds.includes(finding.finding_id))
        group.findingIds.push(finding.finding_id);
      groups.set(key, group);
    }
  return [...groups.values()];
}
const lines = (text: string) => text.replace(/\n$/, "").split("\n");
function sourceAt(
  result: EvaluationResult,
  source: EvidenceSnapshot,
  origin: "TARGET_BASE" | "MERGE_BASE" | "PR_HEAD"
) {
  const sha =
    origin === "TARGET_BASE"
      ? result.revision.base_sha
      : origin === "MERGE_BASE"
        ? result.revision.merge_base_sha
        : result.revision.head_sha;
  return (
    source.kind === "CODE" &&
    source.origin === origin &&
    source.commit_sha === sha &&
    source.repository_id === result.revision.repository_id &&
    source.placement === "EXACT" &&
    source.location?.path === source.path &&
    source.location.start_line > 0 &&
    lines(source.excerpt).length ===
      source.location.end_line - source.location.start_line + 1
  );
}
export type Comparison = {
  before: EvidenceSnapshot;
  after: EvidenceSnapshot;
  beforeLines: { line: number; text: string; changed: boolean }[];
  afterLines: { line: number; text: string; changed: boolean }[];
  changedBefore: string[];
  changedAfter: string[];
  truncated: boolean;
};
export function revisionSources(result: EvaluationResult) {
  return result.evidence.filter((source) =>
    (["TARGET_BASE", "MERGE_BASE", "PR_HEAD"] as const).some((origin) =>
      sourceAt(result, source, origin)
    )
  );
}
export function focusedComparisons(
  result: EvaluationResult,
  findings = result.findings
): Comparison[] {
  const relevant = new Set(
    result.plan
      .filter((unit) =>
        findings.some((finding) => finding.unit_id === unit.unit_id)
      )
      .map((unit) => unit.path)
  );
  const comparisons: Comparison[] = [];
  for (const path of relevant) {
    const heads = result.evidence
      .filter(
        (source) => source.path === path && sourceAt(result, source, "PR_HEAD")
      )
      .sort((a, b) => b.excerpt.length - a.excerpt.length);
    for (const after of heads) {
      const before = result.evidence.find(
        (source) =>
          source.path === path &&
          sourceAt(result, source, "TARGET_BASE") &&
          source.location?.start_line === after.location?.start_line &&
          source.location?.end_line === after.location?.end_line
      );
      if (!before || before.excerpt === after.excerpt) continue;
      const oldLines = lines(before.excerpt),
        newLines = lines(after.excerpt);
      let prefix = 0,
        suffix = 0;
      while (
        prefix < Math.min(oldLines.length, newLines.length) &&
        oldLines[prefix] === newLines[prefix]
      )
        prefix++;
      while (
        suffix < Math.min(oldLines.length, newLines.length) - prefix &&
        oldLines[oldLines.length - 1 - suffix] ===
          newLines[newLines.length - 1 - suffix]
      )
        suffix++;
      const rows = (values: string[], source: EvidenceSnapshot) =>
        values
          .slice(
            Math.max(0, prefix - 2),
            Math.min(values.length, values.length - suffix + 2)
          )
          .map((text, index) => {
            const offset = Math.max(0, prefix - 2) + index;
            return {
              text,
              line: source.location!.start_line + offset,
              changed: offset >= prefix && offset < values.length - suffix,
            };
          });
      const beforeRows = rows(oldLines, before),
        afterRows = rows(newLines, after);
      comparisons.push({
        before,
        after,
        beforeLines: beforeRows.slice(0, 16),
        afterLines: afterRows.slice(0, 16),
        changedBefore: oldLines.slice(prefix, oldLines.length - suffix),
        changedAfter: newLines.slice(prefix, newLines.length - suffix),
        truncated: beforeRows.length > 16 || afterRows.length > 16,
      });
      break;
    }
  }
  return comparisons;
}
export function limitationSummary(code: string) {
  if (/CONTEXT_TOKEN|SCOPE_CONTEXT/.test(code))
    return "Some source context was shortened to fit the retained review scope.";
  if (/CONSTRAINT_LIMIT|DECISION_LIMIT/.test(code))
    return "Some architectural requirements or decisions fell outside the bounded discovery scope.";
  if (/RELATIONSHIP/.test(code))
    return "Not every related dependency path could be explored.";
  if (/BUDGET|TOKEN/.test(code))
    return "Available review resources limited the assessed scope.";
  return "Additional discovery or source context remains unresolved. See technical limitations for the original record.";
}
