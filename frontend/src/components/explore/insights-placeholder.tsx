import { Skeleton } from "@/components/ui/skeleton";

type InsightsPlaceholderProps = {};

export function InsightsPlaceholder({}: InsightsPlaceholderProps) {
  return <Skeleton className="h-full animate-none" />;
}
