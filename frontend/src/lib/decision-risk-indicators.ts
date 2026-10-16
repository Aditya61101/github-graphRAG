import type { DecisionRisk } from "@/types/reviews";

/** Positive, exact decision associations only; absence never establishes safety. */
export function currentDecisionRiskIndicators(
  risks: DecisionRisk[],
  available: boolean
) {
  const indicators = new Map<string, DecisionRisk["classification"]>();
  if (!available) return indicators;
  for (const risk of risks) {
    if (risk.kind !== "DECISION") continue;
    if (
      risk.classification === "CONFIRMED" ||
      (risk.classification === "POTENTIAL" && !indicators.has(risk.identity))
    ) {
      indicators.set(risk.identity, risk.classification);
    }
  }
  return indicators;
}
