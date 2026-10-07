import { Link } from "react-router";
import {
  AlertTriangle,
  CalendarClockIcon,
  FolderKanbanIcon,
  Folders,
  GitBranchIcon,
  PlusIcon,
} from "lucide-react";

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
      <div className="space-y-3">
        {Array.from({ length: 5 }).map((_, index) => (
          <div
            key={index}
            className="flex items-center justify-between gap-6 rounded-2xl border p-2 px-4"
          >
            <div className="flex min-w-0 flex-1 items-center gap-4">
              <Skeleton className="size-12 rounded-2xl" />

              <div className="min-w-0 flex-1 space-y-3">
                <Skeleton className="h-5 w-52" />
                <Skeleton className="h-4 w-full max-w-xl" />
              </div>
            </div>

            <div className="hidden items-center gap-8 lg:flex">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-6 w-20 rounded-full" />
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
    <div className="space-y-3">
      {projects.map((project) => (
        <Link
          key={project.id}
          to={`/projects/${project.id}`}
          className="group block"
        >
          <div className="flex flex-col gap-5 rounded-2xl border border-border/70 bg-card/30 p-2 pr-4 transition-all duration-200 hover:border-primary/25 hover:bg-card/50 hover:shadow-lg hover:shadow-black/5 lg:grid lg:grid-cols-[minmax(0,1.7fr)_240px_170px_100px] lg:items-center lg:gap-8 dark:hover:shadow-black/20">
            <div className="flex min-w-0 items-center gap-4">
              <div className="flex size-12 shrink-0 items-center justify-center rounded-2xl border border-primary/15 bg-primary/5 text-primary transition-colors duration-200 group-hover:border-primary/30 group-hover:bg-primary/10">
                <FolderKanbanIcon className="size-5" />
              </div>

              <div className="min-w-0 flex-1">
                <h3 className="truncate text-base font-semibold tracking-tight">
                  {project.name}
                </h3>
                <p className="line-clamp-1 text-sm text-muted-foreground">
                  {project.description || "No project description available."}
                </p>
              </div>
            </div>

            <div className="flex min-w-0 items-center gap-3">
              <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary/5 text-primary">
                <GitBranchIcon className="size-4" />
              </div>

              <div className="min-w-0">
                <p className="text-xs font-medium text-muted-foreground">
                  Repository
                </p>
                <p className="truncate text-sm font-semibold text-foreground">
                  {project.repositoryName}
                </p>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-muted/40 text-muted-foreground">
                <CalendarClockIcon className="size-4" />
              </div>

              <div>
                <p className="text-xs font-medium text-muted-foreground">
                  Updated
                </p>
                <p className="text-sm font-semibold text-foreground">
                  {project.updatedAt}
                </p>
              </div>
            </div>

            <div className="flex lg:justify-end">
              <div className="inline-flex items-center rounded-full bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400">
                Active
              </div>
            </div>
          </div>
        </Link>
      ))}
    </div>
  );
}
