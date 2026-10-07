import type { ReactNode } from "react";
import { PackageOpen, type LucideIcon } from "lucide-react";

type EmptyStateProps = {
  Icon?: LucideIcon;
  text?: string;
  subtext?: string;
  actionNode?: ReactNode;
};

export function EmptyState({
  Icon = PackageOpen,
  text = "Nothing here yet!",
  subtext = "Try adding new items or refreshing the list.",
  actionNode,
}: EmptyStateProps) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center">
      <Icon className="mb-4 size-16 text-muted-foreground" />
      <div className="mb-6">
        <h4 className="text-center">{text}</h4>
        <p className="text-center text-sm text-muted-foreground">{subtext}</p>
      </div>
      {actionNode}
    </div>
  );
}
