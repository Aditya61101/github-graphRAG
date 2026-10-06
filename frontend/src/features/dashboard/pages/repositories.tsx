import { useMemo, useState } from "react";
import { CalendarClock, GitBranch, Lock, Star } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { toast } from "@/components/ui/toast";
import { useApi } from "@/hooks/use-api";
import { repositoryService } from "@/features/repositories/api";
import { EmptyState } from "@/components/empty-state";
import { Skeleton } from "@/components/ui/skeleton";

export default function RepositoriesPage() {
  const [selectedRepositoryId, setSelectedRepositoryId] = useState<string>();
  const [isIngesting, setIsIngesting] = useState(false);

  const {
    data: repositories,
    loading,
    execute: reloadRepositories,
  } = useApi(repositoryService.getRepositories, {
    initialData: [],
    immediate: true,
  });

  const selectedRepository = useMemo(
    () => repositories.find((repo) => repo.id === selectedRepositoryId),
    [repositories, selectedRepositoryId]
  );

  const handleIngestion = async () => {
    if (!selectedRepository) {
      return;
    }

    setIsIngesting(true);

    await new Promise((resolve) => setTimeout(resolve, 1800));

    setIsIngesting(false);
    toast.add({
      type: "success",
      description: "Repository queued for ingestion successfully.",
    });
  };

  return (
    <div className="flex flex-1 flex-col gap-6 p-4 pt-0">
      <section className="flex items-center justify-between gap-2">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            GitHub repositories
          </h1>
          <p className="max-w-3xl text-sm text-muted-foreground">
            Select a repository from your connected GitHub account and send it
            for ingestion into the GraphRAG pipeline.
          </p>
        </div>
        <Button
          disabled={!selectedRepository || isIngesting}
          onClick={handleIngestion}
        >
          {isIngesting ? "Ingesting repository..." : "Ingest repository"}
        </Button>
      </section>

      {loading ? (
        <div className="grid gap-4 lg:grid-cols-3">
          {Array.from({ length: 12 }).map((_, index) => (
            <Skeleton key={index} className="h-36" />
          ))}
        </div>
      ) : repositories.length > 0 ? (
        <div className="grid gap-4 lg:grid-cols-3">
          {repositories.map((repository) => {
            const isSelected = repository.id === selectedRepositoryId;

            return (
              <button
                key={repository.id}
                type="button"
                onClick={() => setSelectedRepositoryId(repository.id)}
                className="text-left"
              >
                <Card
                  className={`h-full cursor-pointer transition-all duration-200 hover:ring-primary/70 ${
                    isSelected
                      ? "shadow-md ring-2 ring-primary"
                      : "hover:shadow-sm"
                  }`}
                >
                  <CardHeader>
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <CardTitle className="flex items-center gap-2">
                          <GitBranch className="size-4" />
                          {repository.name}
                        </CardTitle>
                        <CardDescription className="mt-2 line-clamp-2">
                          {repository.description}
                        </CardDescription>
                      </div>

                      <div className="rounded-full border px-2 py-1 text-xs font-medium text-muted-foreground">
                        {repository.isPrivate ? (
                          <span className="flex items-center gap-1">
                            <Lock className="size-3" />
                            Private
                          </span>
                        ) : (
                          "Public"
                        )}
                      </div>
                    </div>
                  </CardHeader>

                  <CardContent>
                    <div className="flex flex-wrap items-center justify-between gap-4 text-sm text-muted-foreground">
                      <div className="flex items-center gap-1.5">
                        <Star className="size-4 text-yellow-500" />
                        <span>NA stars</span>
                      </div>

                      <div className="flex items-center gap-1.5">
                        <CalendarClock className="size-4" />
                        <span>Updated {repository.updatedAt}</span>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </button>
            );
          })}
        </div>
      ) : (
        <EmptyState
          text="No repositories found"
          subtext="Make sure you have at least one repository available."
          actionLabel="Reload"
          action={reloadRepositories}
        />
      )}
    </div>
  );
}
