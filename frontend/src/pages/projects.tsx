import { useQuery } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ProjectsList } from "@/components/projects/projects-list";
import { projectService } from "@/api/project-service";

export default function ProjectsPage() {
  const {
    data: projects,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["projects"],
    queryFn: projectService.getProjects,
  });

  const handleCreateProject = () => {};

  return (
    <div className="flex flex-1 flex-col gap-6 p-4 pt-0">
      <section className="flex items-center justify-between gap-2">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            Your projects
          </h1>
          <p className="max-w-3xl text-sm text-muted-foreground">
            Create and manage projects to organize repositories and explore
            GraphRAG insights.
          </p>
        </div>
        <Button onClick={handleCreateProject}>
          <PlusIcon />
          Create project
        </Button>
      </section>

      <ProjectsList
        projects={projects}
        isLoading={isLoading}
        isError={isError}
        error={error}
        onCreateProject={handleCreateProject}
      />
    </div>
  );
}
