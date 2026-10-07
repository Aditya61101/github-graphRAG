import { BotIcon, SendIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";

export default function ExplorePage() {
  return (
    <div className="flex flex-1 flex-col gap-6 overflow-hidden p-4 pt-0">
      <section className="grid min-h-0 flex-1 gap-4 overflow-hidden xl:grid-cols-[1fr_1fr]">
        <div className="flex min-h-0 flex-1 flex-col rounded-xl border bg-background">
          <div className="border-b px-4 py-3">
            <div className="flex items-center gap-3">
              <div className="flex size-10 items-center justify-center rounded-full bg-primary/10 text-primary">
                <BotIcon className="size-5" />
              </div>

              <div>
                <h2 className="font-medium">DecisionGuard Agent</h2>
                <p className="text-sm text-muted-foreground">
                  Ask questions about repositories, code relationships, and
                  insights.
                </p>
              </div>
            </div>
          </div>

          <div className="flex flex-1 flex-col justify-end p-4">
            <div className="max-w-[85%] rounded-2xl bg-muted p-4 text-sm">
              Hello! Ask me anything about your repositories and GraphRAG
              workspace.
            </div>
          </div>

          <div className="border-t p-4">
            <div className="flex items-center gap-2">
              <Input placeholder="Ask the agent something..." />

              <Button size="icon">
                <SendIcon className="size-4" />
              </Button>
            </div>
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col rounded-xl border p-1">
          <Skeleton className="h-full animate-none opacity-50" />
        </div>
      </section>
    </div>
  );
}
