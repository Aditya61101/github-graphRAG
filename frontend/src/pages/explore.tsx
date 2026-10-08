import { AlertTriangle, BotIcon, FileQuestion, SendIcon } from "lucide-react";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router";

import { projectService } from "@/api/project-service";
import { AppHeader } from "@/components/core/app-header";
import { EmptyState } from "@/components/core/feedback/empty-state";
import { ErrorState } from "@/components/core/feedback/error-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";

function ExplorePageSkeleton() {
  return (
    <>
      <div className="border-b px-4 py-4">
        <div className="flex items-center gap-2">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 w-4" />
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-4 w-4" />
          <Skeleton className="h-4 w-20" />
        </div>
      </div>

      <div className="flex flex-1 flex-col gap-6 overflow-hidden p-4 pt-0">
        <section className="grid min-h-0 flex-1 gap-4 overflow-hidden xl:grid-cols-[1fr_1fr]">
          <div className="flex min-h-0 flex-1 flex-col rounded-xl border bg-background p-4">
            <div className="flex items-center gap-3 border-b pb-4">
              <Skeleton className="size-10 rounded-full" />

              <div className="space-y-2">
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-3 w-72" />
              </div>
            </div>

            <div className="flex flex-1 flex-col justify-end gap-3 py-4">
              <Skeleton className="h-16 w-[85%] rounded-2xl" />
              <Skeleton className="ml-auto h-12 w-[65%] rounded-2xl" />
            </div>

            <div className="flex items-center gap-2 border-t pt-4">
              <Skeleton className="h-10 flex-1" />
              <Skeleton className="size-10" />
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col rounded-xl border p-4">
            <Skeleton className="h-full w-full" />
          </div>
        </section>
      </div>
    </>
  );
}

export default function ExplorePage() {
  const { projectId } = useParams();

  if (!projectId) {
    return <ErrorState />;
  }

  const {
    data: project,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["projects", projectId],
    queryFn: () => projectService.getProjectById(projectId),
  });

  if (isLoading) {
    return <ExplorePageSkeleton />;
  }

  if (isError) {
    return (
      <ErrorState
        Icon={AlertTriangle}
        text="Unable to load project"
        subtext="Something went wrong while loading the project details."
      />
    );
  }

  if (!project) {
    return (
      <EmptyState
        Icon={FileQuestion}
        text="Project not found"
        subtext="This project doesn't exist or is currently inaccessible."
      />
    );
  }

  const headerCrumbs = [
    { label: "Projects", pathname: "/projects" },
    { label: project.name, pathname: `/projects/${projectId}` },
    { label: "Explore", pathname: `/projects/${projectId}/explore` },
  ];

  return (
    <>
      <AppHeader crumbs={headerCrumbs} />
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
    </>
  );
}
