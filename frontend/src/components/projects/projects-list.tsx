import { Link } from "react-router";
import { AlertTriangle, Folders, PlusIcon } from "lucide-react";

import { ProjectCard } from "@/components/projects/project-card";
import { EmptyState } from "@/components/core/feedback/empty-state";
import { ErrorState } from "@/components/core/feedback/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import type { Project } from "@/types/project";

type ProjectsListProps = {
  projects?: Project[];
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  onCreateProject: () => void;
};

export function ProjectsList({
  projects,
  isLoading,
  isError,
  error,
  onCreateProject,
}: ProjectsListProps) {
  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 8 }).map((_, index) => (
          <div key={index} className="space-y-4 rounded-xl border p-4">
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-4/5" />
            <div className="flex items-center justify-between pt-2">
              <Skeleton className="h-4 w-20" />
              <Skeleton className="h-8 w-8 rounded-full" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <ErrorState
        Icon={AlertTriangle}
        text="Unable to load projects"
        subtext={
          error instanceof Error
            ? error.message
            : "Something went wrong while loading the projects list."
        }
      />
    );
  }

  if (!projects || projects.length === 0) {
    return (
      <EmptyState
        Icon={Folders}
        text="No projects yet"
        subtext="Create your first project to start organizing and exploring repositories."
        actionNode={
          <Button onClick={onCreateProject}>
            <PlusIcon />
            Create project
          </Button>
        }
      />
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {projects.map((project) => (
        <Link key={project.id} to={`/projects/${project.id}`}>
          <ProjectCard project={project} />
        </Link>
      ))}
    </div>
  );
}
