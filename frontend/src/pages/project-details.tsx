import { Link, useParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  BrainCircuitIcon,
  DatabaseZapIcon,
  FileQuestion,
  FileTextIcon,
  FolderSyncIcon,
  GlobeIcon,
  HardDriveUploadIcon,
  NetworkIcon,
  ShieldAlertIcon,
} from "lucide-react";

import { projectService } from "@/api/project-service";
import { AppHeader } from "@/components/core/app-header";
import { EmptyState } from "@/components/core/feedback/empty-state";
import { ErrorState } from "@/components/core/feedback/error-state";
import { Button } from "@/components/ui/button";
import { GithubIcon } from "@/components/icons";

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

  const headerCrumbs = [
    { label: "Projects", pathname: "/projects" },
    ...(project
      ? [
          {
            label: project.name,
            pathname: `/projects/${projectId}`,
          },
        ]
      : []),
  ];

  if (isLoading) {
    return null;
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

  const metrics = [
    {
      label: "Repositories",
      value: "1",
      icon: GithubIcon,
      description: project.repositoryName,
    },
    {
      label: "Indexing status",
      value: "Indexed",
      icon: DatabaseZapIcon,
      description: "Knowledge graph synced",
    },
    {
      label: "Entities",
      value: "12.4K",
      icon: BrainCircuitIcon,
      description: "Nodes extracted",
    },
    {
      label: "Relationships",
      value: "48.9K",
      icon: NetworkIcon,
      description: "Graph edges generated",
    },
    {
      label: "ADR records",
      value: "18",
      icon: FileTextIcon,
      description: "Connected architecture docs",
    },
  ];

  const integrations = [
    {
      title: "Confluence",
      description:
        "Sync architecture decision records and engineering documentation.",
      icon: GlobeIcon,
      connected: true,
      status: "12 synced pages",
    },
    {
      title: "Google Drive",
      description:
        "Attach folders containing ADRs, RFCs, and internal documentation.",
      icon: FolderSyncIcon,
      connected: false,
      status: "Not connected",
    },
    {
      title: "Local upload",
      description:
        "Upload markdown, PDF, or text ADR documents directly to the project.",
      icon: HardDriveUploadIcon,
      connected: true,
      status: "6 uploaded files",
    },
  ];

  return (
    <>
      <AppHeader crumbs={headerCrumbs} />

      <div className="flex flex-1 flex-col gap-5 p-4 pt-0">
        <section className="rounded-xl border bg-background p-5">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="rounded-full bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400">
                  Active
                </span>

                <span className="rounded-full border px-3 py-1 text-xs text-muted-foreground">
                  Updated {project.updatedAt}
                </span>
              </div>

              <div>
                <h1 className="text-2xl font-semibold tracking-tight">
                  {project.name}
                </h1>

                <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
                  {project.description}
                </p>
              </div>
            </div>

            <Link to={`/projects/${projectId}/explore`}>
              <Button>Explore Workspace</Button>
            </Link>
          </div>
        </section>

        <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5">
          {metrics.map((metric) => {
            const Icon = metric.icon;

            return (
              <div
                key={metric.label}
                className="rounded-xl border bg-background p-4"
              >
                <div className="mb-4 flex items-start justify-between">
                  <div>
                    <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                      {metric.label}
                    </p>

                    <p className="mt-2 text-2xl font-semibold">
                      {metric.value}
                    </p>
                  </div>

                  <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <Icon className="size-5" />
                  </div>
                </div>

                <p className="text-sm text-muted-foreground">
                  {metric.description}
                </p>
              </div>
            );
          })}
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.5fr_1fr]">
          <div className="rounded-xl border bg-background">
            <div className="border-b px-5 py-4">
              <h2 className="text-lg font-semibold">Connected repository</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Repository currently associated with this knowledge graph
                workspace.
              </p>
            </div>

            <div className="space-y-4 p-5">
              <div className="flex items-start justify-between rounded-xl border p-4">
                <div className="flex items-start gap-4">
                  <div className="flex size-11 items-center justify-center rounded-lg bg-violet-500/20 text-violet-500">
                    <GithubIcon className="size-5" />
                  </div>

                  <div>
                    <h3 className="font-medium">{project.repositoryName}</h3>

                    <p className="mt-1 text-sm text-muted-foreground">
                      Primary repository used for ingestion, graph expansion,
                      retrieval augmentation, and repository analytics.
                    </p>
                  </div>
                </div>

                <div className="rounded-full bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400">
                  Indexed
                </div>
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">
                    Embedding model
                  </p>
                  <p className="mt-1 text-sm font-medium">
                    text-embedding-3-large
                  </p>
                </div>

                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">
                    Last ingestion
                  </p>
                  <p className="mt-1 text-sm font-medium">2 hours ago</p>
                </div>

                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">
                    Retrieval mode
                  </p>
                  <p className="mt-1 text-sm font-medium">Hybrid GraphRAG</p>
                </div>
              </div>
            </div>
          </div>

          <div className="rounded-xl border bg-background">
            <div className="border-b px-5 py-4">
              <h2 className="text-lg font-semibold">ADR sources</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Connect external systems to enrich the knowledge graph with
                architecture decisions.
              </p>
            </div>

            <div className="space-y-3 p-5">
              {integrations.map((integration) => {
                const Icon = integration.icon;

                return (
                  <div
                    key={integration.title}
                    className="rounded-xl border p-4"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-start gap-3">
                        <div className="flex size-10 items-center justify-center rounded-lg bg-muted">
                          <Icon className="size-5" />
                        </div>

                        <div>
                          <h3 className="font-medium">{integration.title}</h3>

                          <p className="mt-1 text-sm text-muted-foreground">
                            {integration.description}
                          </p>

                          <p className="mt-3 text-xs font-medium text-muted-foreground">
                            {integration.status}
                          </p>
                        </div>
                      </div>

                      <Button
                        variant={integration.connected ? "outline" : "default"}
                        size="sm"
                      >
                        {integration.connected ? "Manage" : "Connect"}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </section>

        <section className="rounded-xl border border-destructive/40 bg-background">
          <div className="border-b border-destructive/20 px-5 py-4">
            <div className="flex items-center gap-2 text-destructive">
              <ShieldAlertIcon className="size-5" />
              <h2 className="text-lg font-semibold">Danger zone</h2>
            </div>

            <p className="mt-1 text-sm text-muted-foreground">
              Permanently remove this project, indexed entities, relationships,
              embeddings, and connected ADR sources.
            </p>
          </div>

          <div className="flex flex-col gap-4 p-5 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h3 className="font-medium text-destructive">Delete project</h3>

              <p className="mt-1 text-sm text-muted-foreground">
                This action is irreversible and will permanently erase the
                GraphRAG workspace and all associated indexed data.
              </p>
            </div>

            <Button variant="destructive">Delete project</Button>
          </div>
        </section>
      </div>
    </>
  );
}
