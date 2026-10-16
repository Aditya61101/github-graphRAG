import { Link } from "react-router";
import type { DecisionRisk } from "@/types/reviews";
import { prPath } from "@/lib/review-presentation";

export function DecisionRisks({
  risks,
  repositoryId,
  total,
  available = true,
}: {
  risks: DecisionRisk[];
  repositoryId: string;
  total: number;
  available?: boolean;
}) {
  return (
    <div className="space-y-3 text-sm">
      {risks.map((risk) => (
        <article
          key={`${risk.kind}:${risk.identity}`}
          className="space-y-2 rounded-lg border p-3"
        >
          <p className="font-medium">
            {risk.kind === "REQUIREMENT"
              ? "Requirement without an explicit decision mapping"
              : "Architectural decision"}{" "}
            ·{" "}
            {risk.classification === "CONFIRMED"
              ? "Confirmed conflict"
              : "Potential risk"}
          </p>
          <p className="text-muted-foreground">
            {risk.finding_ids.length} associated governing-rule finding(s).{" "}
            {risk.kind === "REQUIREMENT"
              ? "Not included in the distinct decision risk count."
              : "Associated with currently observed open PR evidence."}
          </p>
          <div className="flex flex-wrap gap-3">
            {risk.pr_numbers.map((number) => (
              <Link
                key={number}
                to={prPath(repositoryId, number)}
                className="text-primary underline dark:text-sidebar-primary"
              >
                Review PR #{number}
              </Link>
            ))}
          </div>
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">
              Technical association identities
            </summary>
            <pre className="overflow-auto text-xs break-words whitespace-pre-wrap">
              {JSON.stringify(risk, null, 2)}
            </pre>
          </details>
        </article>
      ))}
      {!risks.length && (
        <p>
          {available
            ? "No currently observed PR risk associations."
            : "Decision risk associations are unavailable."}
        </p>
      )}
      {total > risks.length && (
        <p>
          Showing {risks.length} of {total} risk associations. Review the PR
          list for further evaluations.
        </p>
      )}
    </div>
  );
}
