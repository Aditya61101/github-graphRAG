import { Link, useParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, FileQuestion } from "lucide-react";

import { projectService } from "@/api/project-service";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/core/feedback/error-state";
import { EmptyState } from "@/components/core/feedback/empty-state";
import { Button } from "@/components/ui/button";

export default function ProjectDetailsPage() {
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
    return (
      <div className="flex flex-1 flex-col gap-6 p-4 pt-0">
        <section className="space-y-3">
          <Skeleton className="h-8 w-72" />
          <Skeleton className="h-4 w-full max-w-2xl" />
          <Skeleton className="h-4 w-full max-w-xl" />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-40 rounded-xl" />
          ))}
        </section>

        <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Skeleton className="h-84 rounded-xl xl:col-span-2" />
          <Skeleton className="h-84 rounded-xl" />
        </section>
      </div>
    );
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

  return (
    <div className="flex flex-1 flex-col gap-4 p-4 pt-0">
      <section className="mb-4 flex items-center justify-between gap-2">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {project.name}
          </h1>
          <p className="max-w-3xl text-sm text-muted-foreground">
            {project.description}
          </p>
        </div>
        <Link to={`/projects/${projectId}/explore`}>
          <Button>Explore</Button>
        </Link>
      </section>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-40 animate-none rounded-xl" />
        ))}
      </section>

      <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Skeleton className="h-84 animate-none rounded-xl xl:col-span-2" />
        <Skeleton className="h-84 animate-none rounded-xl" />
      </section>
    </div>
  );
}
