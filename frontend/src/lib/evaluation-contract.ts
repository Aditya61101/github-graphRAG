import schema from "@/types/evaluation.schema.json";
import type {
  EvaluationResult,
  PullRequestDetail,
  PullRequestPage,
} from "@/types/evaluation.generated";

type Schema = {
  $ref?: string;
  anyOf?: Schema[];
  const?: unknown;
  enum?: unknown[];
  type?: string;
  properties?: Record<string, Schema>;
  required?: string[];
  items?: Schema;
  additionalProperties?: boolean;
  minimum?: number;
  maximum?: number;
};
const definitions: Record<string, Schema> = schema.$defs;
function matches(value: unknown, rule: Schema): boolean {
  if (rule.$ref)
    return matches(value, definitions[rule.$ref.split("/").at(-1)!]);
  if (rule.anyOf) return rule.anyOf.some((item) => matches(value, item));
  if ("const" in rule && value !== rule.const) return false;
  if (rule.enum && !rule.enum.includes(value)) return false;
  switch (rule.type) {
    case "null":
      return value === null;
    case "string":
      return typeof value === "string";
    case "boolean":
      return typeof value === "boolean";
    case "integer":
    case "number":
      return (
        typeof value === "number" &&
        Number.isFinite(value) &&
        (rule.type !== "integer" || Number.isInteger(value)) &&
        (rule.minimum === undefined || value >= rule.minimum) &&
        (rule.maximum === undefined || value <= rule.maximum)
      );
    case "array":
      return (
        Array.isArray(value) &&
        value.every((item) => matches(item, rule.items ?? {}))
      );
    case "object": {
      if (!value || typeof value !== "object" || Array.isArray(value))
        return false;
      const object = value as Record<string, unknown>;
      return (
        Object.keys(rule.properties ?? {}).every((key) => key in object) &&
        Object.entries(object).every(([key, item]) =>
          rule.properties?.[key]
            ? matches(item, rule.properties[key])
            : rule.additionalProperties !== false
        )
      );
    }
    default:
      return true;
  }
}
export function parseContract<T>(name: string, value: unknown): T {
  if (!definitions[name] || !matches(value, definitions[name]))
    throw new Error(
      "The server returned an incompatible architectural review response."
    );
  return value as T;
}
function validatePage(total: number, limit: number, offset: number) {
  if (total < 0 || limit < 1 || offset < 0)
    throw new Error("The server returned incompatible pagination.");
}
export const parsePage = (value: unknown) => {
  const page = parseContract<PullRequestPage>("PullRequestPage", value);
  validatePage(page.total, page.limit, page.offset);
  return page;
};
export const parseDetail = (value: unknown) => {
  const detail = parseContract<PullRequestDetail>("PullRequestDetail", value);
  validatePage(detail.history_total, detail.limit, detail.offset);
  return detail;
};
export const parseResult = (value: unknown) =>
  parseContract<EvaluationResult>("EvaluationResult", value);
