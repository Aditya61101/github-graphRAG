import { createContext, useContext, useEffect, useRef } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useOutletContext,
  useParams,
} from "react-router";
import {
  CancelledError,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import axios from "axios";
import {
  ArrowLeft,
  BookOpen,
  ExternalLink,
  FileText,
  GitBranch,
  GitPullRequest,
  LayoutDashboard,
  LoaderCircle,
  Network,
} from "lucide-react";
import { repositoryService } from "@/api/repository-service";
import { RepositoryHeader } from "@/components/repositories/repository-header";
import { Button } from "@/components/ui/button";
import {
  ContentLoading,
  RefreshButton,
} from "@/components/core/query-feedback";
import { isAuthoritativeAccessError } from "@/lib/api-error";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { useAuth } from "@/contexts/auth-context";
import type { RepositoryOverview } from "@/types/overview";
import {
  adrStateOptions,
  finishADRRecovery,
  isGraphReadinessError,
  readADRState,
} from "@/lib/adr-reconciliation";

export function useRepositoryOverview() {
  return useOutletContext<RepositoryOverview>();
}

const OverviewRefreshContext = createContext({
  busy: false,
  refresh: () => {},
});
export function useOverviewRefresh() {
  return useContext(OverviewRefreshContext);
}

function RepositorySidebar({
  overview,
  repositoryId,
}: {
  overview?: RepositoryOverview;
  repositoryId: string;
}) {
  const { setOpen, setOpenMobile, isMobile } = useSidebar();
  const { pathname } = useLocation();
  const previousPath = useRef<string | null>(null);
  useEffect(() => {
    if (previousPath.current === pathname) return;
    previousPath.current = pathname;
    if (pathname.endsWith("/explore")) {
      setOpen(false);
      setOpenMobile(false);
    }
  }, [pathname, setOpen, setOpenMobile]);
  const items = [
    ["overview", "Overview", LayoutDashboard],
    ["explore", "Knowledge Explorer", Network],
    ["decisions", "Decisions", FileText],
    ["pull-requests", "Pull Requests", GitPullRequest],
  ] as const;
  const closeDrawer = () => {
    if (isMobile) setOpenMobile(false);
  };
  return (
    <Sidebar
      collapsible="icon"
      className="top-16 h-[calc(100svh-4rem)]"
      aria-label="Repository navigation"
    >
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip={overview?.repository.full_name ?? "Repository workspace"}
            >
              <BookOpen aria-hidden="true" />
              <span className="truncate">
                {overview?.repository.full_name ?? "Repository workspace"}
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <SidebarMenu>
          {items.map(([path, label, Icon]) => (
            <SidebarMenuItem key={path}>
              <SidebarMenuButton
                render={
                  <NavLink
                    to={`/projects/${encodeURIComponent(repositoryId)}/${path}`}
                    onClick={closeDrawer}
                  />
                }
                isActive={
                  pathname ===
                    `/projects/${encodeURIComponent(repositoryId)}/${path}` ||
                  pathname.startsWith(
                    `/projects/${encodeURIComponent(repositoryId)}/${path}/`
                  )
                }
                tooltip={label}
                aria-label={label}
              >
                <Icon aria-hidden="true" />
                <span>{label}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link to="/projects" onClick={closeDrawer} />}
              tooltip="All Repositories"
            >
              <ArrowLeft aria-hidden="true" />
              <span>All Repositories</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}

export default function RepositoryLayout() {
  const queryClient = useQueryClient();
  const { projectId = "" } = useParams();
  const { user, token } = useAuth();
  const location = useLocation();
  const isExplorer = location.pathname.endsWith("/explore");
  const isPRDetails = /\/pull-requests\/[^/]+\/?$/.test(location.pathname);
  const validId = /^repo_[A-Za-z0-9_-]+$/.test(projectId);
  const scope = [user?.id, token, projectId];
  const adrState = useQuery(adrStateOptions(scope));
  const overview = useQuery({
    queryKey: ["repository-overview", user?.id, token, projectId],
    queryFn: async ({ signal }) => {
      const started = readADRState(queryClient, scope);
      const result = await repositoryService.getOverview(projectId, signal);
      if (signal.aborted) throw new CancelledError();
      finishADRRecovery(queryClient, scope, started, result);
      return result;
    },
    enabled: validId && Boolean(user && token) && !adrState.data.cleanup,
    retry: false,
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const data = overview.data;
  const blockingError =
    overview.isError &&
    (!data ||
      isAuthoritativeAccessError(overview.error) ||
      Boolean(adrState.data.cleanup));
  const updating =
    isGraphReadinessError(overview.error) ||
    (Boolean(adrState.data.cleanup) && !overview.isError);
  const status = axios.isAxiosError(overview.error)
    ? overview.error.response?.status
    : undefined;
  const errorTitle = !validId
    ? "Invalid repository ID"
    : status === 404
      ? "Repository not found"
      : status === 403
        ? "Repository access denied"
        : "Unable to load repository";
  return (
    <div
      className={
        isExplorer
          ? "flex h-dvh min-h-0 flex-col overflow-hidden bg-background"
          : "min-h-svh bg-background"
      }
    >
      <div className="sticky top-0 z-20 shrink-0">
        <RepositoryHeader />
      </div>
      <SidebarProvider
        defaultOpen={!location.pathname.endsWith("/explore")}
        className={
          isExplorer
            ? "min-h-0 flex-1 overflow-hidden"
            : "min-h-[calc(100svh-4rem)]"
        }
      >
        <RepositorySidebar
          overview={blockingError ? undefined : data}
          repositoryId={projectId}
        />
        <SidebarInset
          className={isExplorer ? "min-h-0 min-w-0 overflow-hidden" : "min-w-0"}
        >
          <div className="flex shrink-0 items-center gap-3 border-b px-4 py-3 sm:px-6">
            <SidebarTrigger />
            <nav
              aria-label="Breadcrumb"
              className="flex min-w-0 items-center gap-2 text-sm"
            >
              <Link
                className="shrink-0 text-muted-foreground hover:text-foreground"
                to="/projects"
              >
                All repositories
              </Link>
              <span aria-hidden="true">/</span>
              <span className="truncate">
                {!blockingError && data
                  ? data.repository.full_name
                  : "Repository"}
              </span>
            </nav>
          </div>
          <main
            className={
              isExplorer
                ? "min-h-0 w-full flex-1 overflow-hidden px-4 py-4 sm:px-6"
                : isPRDetails
                  ? "w-full min-w-0 space-y-6 p-4 sm:p-6 lg:p-8"
                  : "mx-auto w-full max-w-6xl space-y-6 p-4 sm:p-6 lg:p-8"
            }
          >
            {validId && updating ? (
              <section
                role="status"
                className="space-y-3 rounded-xl border bg-card p-6"
              >
                <h1 className="text-xl font-semibold">
                  Updating repository knowledge
                </h1>
                <p className="text-sm text-muted-foreground">
                  {adrState.data.cleanup
                    ? adrState.data.uncertain
                      ? "Deletion could not be confirmed. The server may already have saved the deletion. Refresh repository knowledge, then review source documents before retrying."
                      : `Deleted ${adrState.data.cleanup}. ADR deleted. Updating repository knowledge. Some views may be temporarily unavailable.`
                    : "Repository graph update or repair is in progress. Some views are temporarily unavailable."}
                </p>
                <p className="text-sm text-muted-foreground">
                  Refresh to check readiness. Background cleanup has not yet
                  been confirmed complete.
                </p>
                <Button
                  variant="outline"
                  aria-busy={overview.isFetching}
                  disabled={overview.isFetching}
                  onClick={() =>
                    void overview.refetch({ cancelRefetch: false })
                  }
                >
                  {overview.isFetching && (
                    <LoaderCircle
                      aria-hidden="true"
                      className="size-4 animate-spin motion-reduce:animate-none"
                    />
                  )}
                  {overview.isFetching
                    ? "Checking readiness..."
                    : "Refresh repository knowledge"}
                </Button>
              </section>
            ) : !validId || blockingError ? (
              <section role="alert" className="space-y-3 rounded-xl border p-6">
                <h1 className="text-xl font-semibold">{errorTitle}</h1>
                <p className="text-sm text-muted-foreground">
                  {status === 403
                    ? "Your current account or GitHub App access does not permit this repository."
                    : status === 404 || !validId
                      ? "Choose a tracked repository from All Repositories."
                      : "Repository access could not be verified. Please try again."}
                </p>
                {validId && status !== 403 && status !== 404 && (
                  <Button
                    variant="outline"
                    aria-busy={overview.isFetching}
                    disabled={overview.isFetching}
                    onClick={() =>
                      void overview.refetch({ cancelRefetch: false })
                    }
                  >
                    Try again
                  </Button>
                )}
              </section>
            ) : overview.isPending || !data ? (
              <ContentLoading label="Loading repository workspace..." cards />
            ) : (
              <>
                {!isExplorer && !isPRDetails && (
                  <section
                    className="flex flex-wrap items-center justify-between gap-3"
                    aria-label="Repository identity"
                  >
                    <div className="space-y-2">
                      <p className="font-medium break-all">
                        {data.repository.owner} / {data.repository.name}
                      </p>
                      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          <GitBranch className="size-3.5" aria-hidden="true" />
                          {data.repository.tracked_branch}
                        </span>
                        <span className="rounded-full border bg-muted/50 px-2 py-1">
                          Indexing: {data.repository.status}
                        </span>
                      </div>
                    </div>
                    <Button
                      nativeButton={false}
                      variant="ghost"
                      size="sm"
                      render={
                        <a
                          href={data.repository.repository_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        />
                      }
                    >
                      View on GitHub <ExternalLink aria-hidden="true" />
                    </Button>
                  </section>
                )}
                {!isExplorer && data.repository.status !== "COMPLETED" && (
                  <p
                    role="status"
                    className="rounded-lg border bg-muted/30 p-3 text-sm text-muted-foreground"
                  >
                    {data.repository.status === "FAILED"
                      ? "The latest indexing attempt failed. Any last successful indexed revision is shown separately below."
                      : "Indexing is incomplete. Persisted decisions may represent a partial repository snapshot."}
                  </p>
                )}
                {overview.isError && (
                  <div
                    role="alert"
                    className="flex flex-wrap items-center gap-3 rounded-lg border bg-card p-3 text-sm"
                  >
                    <p>
                      Unable to refresh repository information. Previously
                      loaded information is shown.
                    </p>
                    <RefreshButton
                      busy={overview.isFetching}
                      label="Retry refresh"
                      onRefresh={() => {
                        void overview.refetch({ cancelRefetch: false });
                      }}
                    />
                  </div>
                )}
                <OverviewRefreshContext.Provider
                  value={{
                    busy: overview.isFetching,
                    refresh: () => {
                      void overview.refetch({ cancelRefetch: false });
                    },
                  }}
                >
                  <Outlet context={data} />
                </OverviewRefreshContext.Provider>
              </>
            )}
          </main>
        </SidebarInset>
      </SidebarProvider>
    </div>
  );
}
