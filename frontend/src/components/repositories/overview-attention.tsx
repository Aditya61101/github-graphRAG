import { Link } from "react-router";
import type { DecisionRisk } from "@/types/reviews";
import { prPath } from "@/lib/review-presentation";

function RiskRow({
  risk,
  repositoryId,
}: {
  risk: DecisionRisk;
  repositoryId: string;
}) {
  const knowledge =
    risk.knowledge_resolution === "RESOLVED" &&
    risk.knowledge?.id === risk.identity &&
    risk.knowledge.kind ===
      (risk.kind === "DECISION" ? "decision" : "constraint")
      ? risk.knowledge
      : null;
  const title = knowledge?.title || knowledge?.description;
  const fallback =
    risk.kind === "DECISION"
      ? "Decision details unavailable"
      : "Requirement details unavailable";
  return (
    <article className="space-y-2 py-4 text-sm" data-risk-kind={risk.kind}>
      <div className="flex flex-wrap items-start justify-between gap-x-5 gap-y-1">
        <h3 className="min-w-0 flex-1 basis-64 font-semibold break-words">
          {title || fallback}
        </h3>
        <span
          className={
            risk.classification === "CONFIRMED"
              ? "text-destructive"
              : "text-amber-700 dark:text-amber-300"
          }
        >
          {risk.classification === "CONFIRMED"
            ? "Confirmed conflict"
            : "Potential risk"}
        </span>
      </div>
      <p className="text-xs break-words text-muted-foreground">
        {knowledge?.sources
          .map((source) => source.title || source.source_name)
          .filter(Boolean)
          .join(" · ") || "Source ADR details unavailable"}{" "}
        · {risk.pr_numbers.map((number) => `PR #${number}`).join(", ")}
      </p>
      {knowledge?.title && knowledge.description && (
        <p
          className="line-clamp-2 break-words text-muted-foreground"
          title={knowledge.description}
        >
          {knowledge.description}
        </p>
      )}
      {!knowledge && (
        <p className="text-xs text-muted-foreground">
          {risk.knowledge_resolution === "NOT_FOUND"
            ? "The exact knowledge record could not be resolved. No inferred mapping is shown."
            : "Knowledge labels are temporarily unavailable. The persisted risk classification is retained."}
        </p>
      )}
      <div className="flex flex-wrap gap-x-4 gap-y-2 text-primary dark:text-sidebar-primary">
        {knowledge && (
          <Link
            className="hover:underline focus-visible:outline-2 focus-visible:outline-offset-4"
            to={`/projects/${encodeURIComponent(repositoryId)}/decisions?record=${encodeURIComponent(knowledge.id)}`}
          >
            View {risk.kind === "DECISION" ? "decision" : "requirement"}
          </Link>
        )}
        {risk.pr_numbers.map((number) => (
          <Link
            key={number}
            className="hover:underline focus-visible:outline-2 focus-visible:outline-offset-4"
            to={prPath(repositoryId, number)}
          >
            Review PR #{number}
          </Link>
        ))}
      </div>
    </article>
  );
}

export function OverviewAttention({
  risks,
  repositoryId,
  total,
  additionalTotal,
  available,
}: {
  risks: DecisionRisk[];
  repositoryId: string;
  total: number;
  additionalTotal: number;
  available: boolean;
}) {
  const decisions = risks.filter((risk) => risk.kind === "DECISION");
  const requirements = risks.filter((risk) => risk.kind === "REQUIREMENT");
  return (
    <div className="min-w-0 px-5">
      <div className="divide-y">
        {decisions.map((risk) => (
          <RiskRow
            key={risk.identity}
            risk={risk}
            repositoryId={repositoryId}
          />
        ))}
      </div>
      {!risks.length && available && (
        <p className="py-4 text-sm text-muted-foreground">
          No currently observed PR risk associations.
        </p>
      )}
      {(requirements.length > 0 || additionalTotal > 0) && (
        <details className="border-t py-3">
          <summary className="cursor-pointer py-1 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">
            {additionalTotal} other affected architectural{" "}
            {additionalTotal === 1 ? "requirement" : "requirements"}
          </summary>
          <p className="mt-2 text-xs text-muted-foreground">
            Without an explicit decision mapping; excluded from Decisions at
            Risk.
          </p>
          <div className="divide-y">
            {requirements.map((risk) => (
              <RiskRow
                key={risk.identity}
                risk={risk}
                repositoryId={repositoryId}
              />
            ))}
          </div>
        </details>
      )}
      {total > risks.length && (
        <p className="py-3 text-xs text-muted-foreground">
          Showing {risks.length} of {total} risk associations. Review the PR
          list for further evaluations.
        </p>
      )}
    </div>
  );
}
