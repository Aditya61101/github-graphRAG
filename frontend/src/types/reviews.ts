import type { EvaluationSummary } from "./evaluation.generated";
import type { KnowledgeItem } from "./knowledge";
export type RecentReview = EvaluationSummary & {
  pr_number: number;
  state: string;
  freshness: string;
  title?: string | null;
  observed_at?: number | null;
};
export type DecisionRisk = {
  identity: string;
  kind: "DECISION" | "REQUIREMENT";
  classification: "CONFIRMED" | "POTENTIAL";
  pr_numbers: number[];
  finding_ids: string[];
  adr_ids: string[];
  knowledge?: KnowledgeItem | null;
  knowledge_resolution?: "RESOLVED" | "NOT_FOUND" | "UNAVAILABLE";
};
export type RiskBreakdown = {
  confirmed_decisions: number;
  potential_decisions: number;
  unmapped_requirements: number;
  confirmed_warnings: number;
  potential_warnings: number;
  uncertain_units: number;
};
