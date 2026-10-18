import { LoaderCircle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export function RefreshButton({
  busy,
  onRefresh,
  label = "Refresh",
}: {
  busy: boolean;
  onRefresh: () => void;
  label?: string;
}) {
  return (
    <Button
      size="sm"
      variant="outline"
      disabled={busy}
      aria-busy={busy}
      onClick={() => {
        if (!busy) onRefresh();
      }}
    >
      {busy ? (
        <LoaderCircle
          aria-hidden="true"
          className="size-4 animate-spin motion-reduce:animate-none"
        />
      ) : (
        <RefreshCw aria-hidden="true" className="size-4" />
      )}
      {busy ? "Refreshing…" : label}
    </Button>
  );
}

export function ContentLoading({
  label,
  cards = false,
}: {
  label: string;
  cards?: boolean;
}) {
  return (
    <section role="status" aria-busy="true" className="space-y-4">
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <LoaderCircle
          aria-hidden="true"
          className="size-4 animate-spin motion-reduce:animate-none"
        />
        {label}
      </p>
      <div
        aria-hidden="true"
        className={cards ? "grid gap-4 sm:grid-cols-2" : "space-y-3"}
      >
        {[0, 1, 2, 3].map((item) => (
          <div key={item} className="space-y-3 rounded-xl border bg-card p-5">
            <Skeleton className="h-4 w-1/3 motion-reduce:animate-none" />
            <Skeleton className="h-6 w-2/3 motion-reduce:animate-none" />
            <Skeleton className="h-3 w-1/2 motion-reduce:animate-none" />
          </div>
        ))}
      </div>
    </section>
  );
}
