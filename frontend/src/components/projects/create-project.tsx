import {
  type Dispatch,
  type PropsWithChildren,
  type SetStateAction,
  useMemo,
  useState,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckIcon,
  GitBranchIcon,
  Loader,
  LockIcon,
  SearchIcon,
} from "lucide-react";

import { projectService } from "@/api/project-service";
import { repositoryService } from "@/api/repository-service";
import { useIsMobile } from "@/hooks/use-mobile";
import { Button } from "@/components/ui/button";
import {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerFooter,
  DrawerHeader,
  DrawerTitle,
  DrawerTrigger,
} from "@/components/ui/drawer";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { GithubIcon } from "@/components/icons";
import { toast } from "@/components/ui/toast";
import { Link } from "react-router";
import { useAuth } from "@/contexts/auth-context";
import { getApiErrorMessage, isAccessDenied } from "@/lib/api-error";

type CreateProjectProps = {
  open: boolean;
  setOpen: Dispatch<SetStateAction<boolean>>;
};

export function CreateProject({
  open,
  setOpen,
  children,
}: PropsWithChildren<CreateProjectProps>) {
  const isMobile = useIsMobile();
  const queryClient = useQueryClient();
  const { user, refreshStatus } = useAuth();

  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");
  const [search, setSearch] = useState("");
  const [selectedRepositoryId, setSelectedRepositoryId] = useState<
    string | undefined
  >();

  const {
    data: repositories = [],
    isLoading,
    isFetching,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ["repositories", user?.id],
    queryFn: ({ signal }) => repositoryService.getRepositories(signal),
    enabled: open && Boolean(user),
    retry: false,
  });
  const selectedRepository = repositories.find(
    (repository) => repository.id === selectedRepositoryId
  );

  const filteredRepositories = useMemo(() => {
    const normalizedSearch = search.toLowerCase();

    return repositories.filter((repository) =>
      repository.fullName.toLowerCase().includes(normalizedSearch)
    );
  }, [repositories, search]);

  const { mutate: handleCreateProject, isPending } = useMutation({
    mutationFn: () =>
      projectService.createProject({
        name: projectName,
        description: projectDescription,
        repositoryId: selectedRepositoryId!,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["projects"],
      });

      setProjectName("");
      setProjectDescription("");
      setSelectedRepositoryId(undefined);
      setSearch("");
      setOpen(false);
      toast.add({
        type: "success",
        title: "Project created successfully!",
      });
    },
    onError: () => {
      toast.add({
        type: "error",
        title: "Failed to create project.",
      });
    },
  });

  return (
    <Drawer
      open={open}
      onOpenChange={setOpen}
      showSwipeHandle={isMobile}
      swipeDirection={isMobile ? "down" : "right"}
    >
      <DrawerTrigger>{children}</DrawerTrigger>

      <DrawerContent className="ml-auto flex h-full w-full max-w-2xl flex-col border-l bg-background">
        <DrawerHeader className="border-b px-6 py-5 text-left">
          <DrawerTitle className="text-2xl font-semibold tracking-tight">
            Create project
          </DrawerTitle>

          <DrawerDescription className="text-sm text-wrap">
            Create a new GraphRAG workspace and connect exactly one GitHub
            repository for ingestion and knowledge graph generation.
          </DrawerDescription>
        </DrawerHeader>

        <div className="scrollbar-macos flex-1 space-y-8 overflow-y-auto px-6 py-6">
          <section className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="project-name">
                Project name<span className="text-destructive">*</span>
              </Label>
              <Input
                id="project-name"
                value={projectName}
                onChange={(event) => setProjectName(event.target.value)}
                placeholder="E.g. Ticket Management System"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="project-description">
                Project description<span className="text-destructive">*</span>
              </Label>
              <Input
                id="project-description"
                value={projectDescription}
                onChange={(event) => setProjectDescription(event.target.value)}
                placeholder="Describe what this project is used for..."
              />
            </div>
          </section>

          <section className="space-y-4">
            <div className="space-y-1">
              <h3 className="font-semibold">
                Connect repository
                <span className="ml-2 text-destructive">*</span>
              </h3>
              <p className="text-sm text-muted-foreground">
                Select a public repository granted to the DecisionGuard GitHub
                App.
              </p>
              <Button variant="link" size="sm" render={<Link to="/install" />}>
                Manage GitHub repository access
              </Button>
            </div>

            <div className="rounded-xl border bg-background">
              <div className="border-b p-3">
                <div className="relative">
                  <SearchIcon className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Search repositories..."
                    className="pl-9"
                  />
                </div>
              </div>

              <div className="scrollbar-macos max-h-92 overflow-y-auto p-2">
                {isLoading ? (
                  <div className="space-y-2 p-2">
                    {Array.from({ length: 6 }).map((_, index) => (
                      <Skeleton key={index} className="h-20 rounded-lg" />
                    ))}
                  </div>
                ) : isError ? (
                  <div className="space-y-3 p-6 text-center">
                    <p role="alert" className="text-sm text-destructive">
                      {getApiErrorMessage(
                        error,
                        "Could not load repositories. Please try again."
                      )}
                    </p>
                    <Button
                      variant="outline"
                      onClick={() => {
                        void refetch();
                        void refreshStatus();
                      }}
                      disabled={isFetching}
                    >
                      Try again
                    </Button>
                    {isAccessDenied(error) && (
                      <Button variant="link" render={<Link to="/install" />}>
                        Check GitHub access
                      </Button>
                    )}
                  </div>
                ) : filteredRepositories.length > 0 ? (
                  filteredRepositories.map((repository) => {
                    const isSelected = selectedRepositoryId === repository.id;

                    return (
                      <button
                        key={repository.id}
                        type="button"
                        onClick={() => setSelectedRepositoryId(repository.id)}
                        className={`flex w-full items-start justify-between gap-4 rounded-lg border p-2 text-left transition-colors ${
                          isSelected
                            ? "border-primary bg-primary/5"
                            : "border-transparent hover:bg-muted/50"
                        }`}
                      >
                        <div className="flex items-start gap-3">
                          <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-violet-500/25">
                            <GithubIcon className="size-5" />
                          </div>

                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <p className="truncate font-medium">
                                {repository.fullName}
                              </p>

                              {repository.isPrivate && (
                                <span className="flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs text-muted-foreground">
                                  <LockIcon className="size-3" />
                                  Private
                                </span>
                              )}
                            </div>

                            <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
                              {repository.description ||
                                "No description provided."}
                            </p>

                            <div className="mt-3 flex items-center gap-2 text-xs text-nowrap text-muted-foreground">
                              <GitBranchIcon className="size-3.5" />
                              {repository.updatedAt
                                ? `Last updated ${new Date(repository.updatedAt).toLocaleDateString("en-GB")}`
                                : "Update date unavailable"}
                              <span>
                                {repository.status.replaceAll("_", " ")}
                              </span>
                            </div>
                          </div>
                        </div>

                        {isSelected && (
                          <div className="flex size-4 items-center justify-center rounded-full bg-primary text-primary-foreground">
                            <CheckIcon className="size-3" />
                          </div>
                        )}
                      </button>
                    );
                  })
                ) : (
                  <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
                    <GithubIcon className="size-10 text-muted-foreground" />
                    <div>
                      <p className="font-medium">
                        {repositories.length
                          ? "No matching repositories"
                          : "No granted public repositories"}
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {repositories.length
                          ? "Try adjusting your search query."
                          : "Choose public repositories for the App on GitHub, then check access again."}
                      </p>
                    </div>
                    {!repositories.length && (
                      <Button variant="outline" render={<Link to="/install" />}>
                        Connect repositories on GitHub
                      </Button>
                    )}
                  </div>
                )}
              </div>
            </div>
          </section>
        </div>

        <DrawerFooter className="border-t px-6 py-4">
          <Button
            onClick={() => handleCreateProject()}
            disabled={
              !projectName.trim() ||
              !projectDescription.trim() ||
              !selectedRepository ||
              isError ||
              isFetching ||
              isPending
            }
          >
            {isPending ? (
              <>
                <Loader className="animate-spin" />
                Creating project...
              </>
            ) : (
              "Create project"
            )}
          </Button>
          <DrawerClose render={<Button variant="outline">Cancel</Button>} />
        </DrawerFooter>
      </DrawerContent>
    </Drawer>
  );
}
