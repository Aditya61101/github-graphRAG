import { ArrowUpIcon, PlusIcon, SparklesIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

export function ChatPanel() {
  return (
    <div className="relative flex h-full flex-col overflow-hidden rounded-lg border border-border/60 bg-card p-2 text-card-foreground shadow-2xl backdrop-blur-xl">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,rgba(61,189,162,0.12),transparent_28%),radial-gradient(circle_at_bottom_right,rgba(59,130,246,0.1),transparent_22%)] opacity-80 dark:opacity-70" />

      <div className="relative flex flex-1 flex-col justify-between gap-4">
        <div className="mx-auto flex max-w-sm flex-1">
          <div className="flex flex-col justify-center text-center">
            <div className="mx-auto mb-6 flex size-10 items-center justify-center rounded-xl border border-border/70 bg-muted/30">
              <SparklesIcon className="size-4 text-primary" />
            </div>

            <h1 className="text-2xl font-semibold tracking-tight">
              Explore your repositories
            </h1>

            <p className="mx-auto mt-4 max-w-xl text-sm text-muted-foreground">
              Ask architectural questions, inspect repository relationships, and
              navigate insights.
            </p>
          </div>
        </div>

        <div className="mx-auto w-full max-w-4xl rounded-lg border">
          <div className="scrollbar-macos rounded-lg border border-border/70 bg-background/80 p-4 shadow-2xl transition-all duration-300">
            <textarea
              className="field-sizing-content max-h-20 w-full resize-none border-none bg-transparent text-sm outline-none placeholder:text-sm placeholder:text-muted-foreground"
              placeholder="Help me understand the repository structure."
            />

            <div className="flex items-center justify-between gap-3">
              <Button variant="ghost" size="icon">
                <PlusIcon className="size-4" />
              </Button>

              <Button size="icon">
                <ArrowUpIcon className="size-4" />
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
