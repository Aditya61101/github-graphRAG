import { AlertTriangle, FileQuestion } from "lucide-react";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router";

import { projectService } from "@/api/project-service";
import { AppHeader } from "@/components/core/app-header";
import { EmptyState } from "@/components/core/feedback/empty-state";
import { ErrorState } from "@/components/core/feedback/error-state";
import { ChatPanel } from "@/components/explore/chat-panel";
import { InsightsPlaceholder } from "@/components/explore/insights-placeholder";
import { Skeleton } from "@/components/ui/skeleton";

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
      <div className="flex flex-1 flex-col overflow-hidden bg-background px-4 pt-0 pb-4">
        <section className="grid min-h-0 flex-1 gap-5 overflow-hidden 2xl:grid-cols-[1fr_1fr]">
          <ChatPanel />

          <div className="hidden min-h-0 xl:block">
            <InsightsPlaceholder />
          </div>
        </section>
      </div>
    </>
  );
}

function ExplorePageSkeleton() {
  return (
    <>
      <div className="mb-4 border-b px-4 py-4">
        <div className="flex items-center gap-2">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 w-4" />
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-4 w-4" />
          <Skeleton className="h-4 w-20" />
        </div>
      </div>

      <div className="flex flex-1 flex-col overflow-hidden px-4 pt-0 pb-4">
        <section className="grid min-h-0 flex-1 gap-5 overflow-hidden 2xl:grid-cols-[1fr_1fr]">
          <Skeleton className="h-full w-full" />
          <Skeleton className="h-full w-full" />
        </section>
      </div>
    </>
  );
}
