import { useQuery } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";

import { useState } from "react";

import { projectService } from "@/api/project-service";
import { AppHeader } from "@/components/core/app-header";
import { CreateProject } from "@/components/projects/create-project";
import { ProjectsList } from "@/components/projects/projects-list";
import { Button } from "@/components/ui/button";

export default function ProjectsPage() {
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);

  const {
    data: projects,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["projects"],
    queryFn: projectService.getProjects,
  });

  const handleCreateProject = () => {
    setIsDrawerOpen(true);
  };

  const headerCrumbs = [
    { label: "Projects", pathname: "/projects" },
  ];

  return (
    <>
      <AppHeader crumbs={headerCrumbs} />

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
          <CreateProject open={isDrawerOpen} setOpen={setIsDrawerOpen}>
            <Button onClick={handleCreateProject}>
              <PlusIcon />
              Create project
            </Button>
          </CreateProject>
        </section>

        <ProjectsList
          projects={projects}
          isLoading={isLoading}
          isError={isError}
          error={error}
          onCreateProject={handleCreateProject}
        />
      </div>
    </>
  );
}
