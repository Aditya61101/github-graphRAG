import { Coverage, External } from "@/components/repositories/review-summary";
import { TechnicalDetails } from "@/components/repositories/technical-details";
import {
  citationSource,
  citationUrl,
  findingGroups,
  label,
  reviewReason,
} from "@/lib/review-presentation";
import type {
  EvaluationResult,
  Finding,
  QuoteRef,
} from "@/types/evaluation.generated";

function Citations({
  citations,
  result,
  fullName,
}: {
  citations: QuoteRef[];
  result: EvaluationResult;
  fullName: string;
}) {
  return (
    <div className="space-y-3">
      {citations.map((citation, index) => {
        const source = citationSource(result, citation);
        if (!source) return <p key={index}>Source evidence unavailable.</p>;
        const url = citationUrl(fullName, source, citation);
        return (
          <figure key={index} className="min-w-0 rounded-lg border p-3">
            <figcaption className="mb-2 text-xs text-muted-foreground">
              {source.kind === "ADR"
                ? (source.source_name ?? "Governing ADR")
                : (source.path ?? "Source evidence")}{" "}
              · {label(source.origin)}{" "}
              {url ? (
                <External href={url}>View source evidence</External>
              ) : (
                " · No verified public source link"
              )}
            </figcaption>
            <pre className="max-h-72 overflow-auto text-xs break-words whitespace-pre-wrap">
              {citation.quote}
            </pre>
          </figure>
        );
      })}
    </div>
  );
}
function FindingCard({
  finding,
  result,
  fullName,
}: {
  finding: Finding;
  result: EvaluationResult;
  fullName: string;
}) {
  const candidate = finding.candidate;
  const headCitation = finding.citations.find((citation) => {
    const source = citationSource(result, citation);
    return (
      source?.kind === "CODE" &&
      source.origin === "PR_HEAD" &&
      citationUrl(fullName, source, citation)
    );
  });
  const headSource = headCitation && citationSource(result, headCitation);
  return (
    <article className="space-y-3 rounded-xl border bg-card p-5">
      <h3 className="font-semibold">{finding.claim}</h3>
      <p className="text-sm text-muted-foreground">
        {label(finding.disposition)}
      </p>
      {headCitation && headSource && (
        <External href={citationUrl(fullName, headSource, headCitation)}>
          View changed code
        </External>
      )}
      {candidate && (
        <div className="space-y-2 text-sm">
          <p>
            <strong>Governing requirement:</strong> {candidate.rule.statement}
          </p>
          <p>
            <strong>Mechanism:</strong> {candidate.mechanism}
          </p>
        </div>
      )}
      {finding.consequence && (
        <p className="text-sm">
          <strong>Consequence:</strong> {finding.consequence}
        </p>
      )}
      {finding.suggested_action && (
        <p className="text-sm">
          <strong>Suggested action:</strong> {finding.suggested_action}
        </p>
      )}
      {finding.review_error && (
        <p className="text-sm">{reviewReason(finding.review_error)}</p>
      )}
      <details className="space-y-3">
        <summary className="cursor-pointer font-medium">
          Original source evidence and governing ADR
        </summary>
        <Citations
          citations={candidate?.rule.citations ?? []}
          result={result}
          fullName={fullName}
        />
        <Citations
          citations={finding.citations}
          result={result}
          fullName={fullName}
        />
      </details>
      <details className="space-y-3">
        <summary className="cursor-pointer font-medium">
          Base, merge-base and head comparison
        </summary>
        {candidate?.revisions.map((revision) => (
          <section key={revision.revision} className="space-y-2 border-l pl-3">
            <h4 className="font-medium">
              {label(revision.revision)}: {label(revision.behavior)}
            </h4>
            <Citations
              citations={revision.citations}
              result={result}
              fullName={fullName}
            />
          </section>
        )) ?? <p>Revision comparison unavailable.</p>}
      </details>
      <details className="space-y-3">
        <summary className="cursor-pointer font-medium">
          Independent challenger review
        </summary>
        {finding.review ? (
          <>
            <p>
              {label(finding.review.verdict)}: {finding.review.explanation}
            </p>
            <p>Tested witness: {finding.review.tested_witness}</p>
            {finding.review_error && (
              <p>
                This review did not resolve the allegation:{" "}
                {reviewReason(finding.review_error)}
              </p>
            )}
            <Citations
              citations={finding.review.citations}
              result={result}
              fullName={fullName}
            />
            {finding.review.revisions.map((revision) => (
              <section key={revision.revision}>
                <h4>
                  {label(revision.revision)}: {label(revision.behavior)}
                </h4>
                <Citations
                  citations={revision.citations}
                  result={result}
                  fullName={fullName}
                />
              </section>
            ))}
          </>
        ) : (
          <p>
            No accepted independent review is recorded.{" "}
            {reviewReason(finding.review_error)}
          </p>
        )}
      </details>
      {finding.missing_facts.length > 0 && (
        <details>
          <summary className="cursor-pointer">Missing evidence</summary>
          {finding.missing_facts.map((fact) => (
            <p key={fact.fact_id} className="mt-2 text-sm">
              {fact.fact} — {fact.relevance}
            </p>
          ))}
        </details>
      )}
      <TechnicalDetails
        title="Technical finding details"
        value={{
          finding,
          proofs: result.proofs.filter((proof) =>
            finding.proof_ids.includes(proof.proof_id)
          ),
        }}
      />
    </article>
  );
}
export function EvaluationEvidence({
  result,
  fullName,
}: {
  result: EvaluationResult;
  fullName: string;
}) {
  const groups = findingGroups(result);
  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">
          Confirmed architectural conflicts ({groups.confirmed.length})
        </h2>
        <p className="text-sm text-muted-foreground">
          Each entry records a supported governing requirement. Multiple
          requirements may concern the same code change; these counts do not
          establish distinct bugs.
        </p>
        {groups.confirmed.map((finding) => (
          <FindingCard
            key={finding.finding_id}
            finding={finding}
            result={result}
            fullName={fullName}
          />
        ))}
        {!groups.confirmed.length && (
          <p>
            No confirmed conflicts in this evaluation. Review coverage and
            unresolved evidence below.
          </p>
        )}
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">
          Additional review required ({groups.review.length})
        </h2>
        {groups.review.map((finding) => (
          <FindingCard
            key={finding.finding_id}
            finding={finding}
            result={result}
            fullName={fullName}
          />
        ))}
        {!groups.review.length && <p>No unresolved allegations recorded.</p>}
        <details className="rounded-xl border p-4">
          <summary className="cursor-pointer font-medium">
            Rejected / invalid proof records ({result.rejected_findings.length})
          </summary>
          <p className="my-3 text-sm">
            These records are retained for audit and are not confirmed
            violations.
          </p>
          {result.rejected_findings.map((record, index) => (
            <div key={record.finding_id} className="my-3 border-t pt-3">
              <p>
                Record {index + 1}: {reviewReason(record.reason)}
              </p>
              <details>
                <summary className="cursor-pointer text-sm">
                  Technical rejection details
                </summary>
                <pre className="max-h-64 overflow-auto text-xs break-words whitespace-pre-wrap">
                  {JSON.stringify(record, null, 2)}
                </pre>
              </details>
            </div>
          ))}
        </details>
      </section>
      {groups.other.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-xl font-semibold">
            Other architectural observations
          </h2>
          {groups.other.map((finding) => (
            <FindingCard
              key={finding.finding_id}
              finding={finding}
              result={result}
              fullName={fullName}
            />
          ))}
        </section>
      )}
      <section className="space-y-3 rounded-xl border p-5">
        <h2 className="text-xl font-semibold">Coverage and limitations</h2>
        <Coverage coverage={result.coverage} />
        {result.limitations.length > 0 && (
          <p className="text-sm">
            Architectural discovery was bounded. Some requirements, source
            context or dependency paths were outside the retained review scope.
          </p>
        )}
        <TechnicalDetails
          title="Unit dispositions and discovery details"
          value={{ units: result.units, limitations: result.limitations }}
        />
      </section>
      <TechnicalDetails
        title="Canonical evaluation evidence (technical inspection)"
        value={result}
      />
    </div>
  );
}
