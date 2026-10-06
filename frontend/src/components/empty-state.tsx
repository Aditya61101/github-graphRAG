import type { ReactNode } from "react";
import { PackageOpenIcon, type LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

type EmptyStateProps = {
  Icon?: LucideIcon;
  text?: string;
  subtext?: string;
  actionLabel?: ReactNode;
  action?: () => void;
};

export function EmptyState({
  Icon = PackageOpenIcon,
  text = "Oops!",
  subtext = "No data available",
  actionLabel,
  action,
}: EmptyStateProps) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center">
      <Icon className="mb-4 size-16 text-muted-foreground" />
      <div className="mb-6">
        <h4 className="text-center">{text}</h4>
        <p className="text-center text-sm text-muted-foreground">{subtext}</p>
      </div>
      {actionLabel && action && <Button onClick={action}>{actionLabel}</Button>}
    </div>
  );
}
