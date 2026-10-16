import { useEffect, useRef } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useOutletContext,
  useParams,
} from "react-router";
import { useQuery } from "@tanstack/react-query";
import axios from "axios";
import {
  ArrowLeft,
  BookOpen,
  ExternalLink,
  FileText,
  GitBranch,
  GitPullRequest,
  LayoutDashboard,
  Network,
  Settings,
} from "lucide-react";
import { repositoryService } from "@/api/repository-service";
import { RepositoryHeader } from "@/components/repositories/repository-header";
import { Button } from "@/components/ui/button";
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

export function useRepositoryOverview() {
  return useOutletContext<RepositoryOverview>();
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
    ...(overview?.permissions.can_view_settings
      ? [["settings", "Settings", Settings] as const]
      : []),
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
  const { projectId = "" } = useParams();
  const { user, token } = useAuth();
  const location = useLocation();
  const isExplorer = location.pathname.endsWith("/explore");
  const isPRDetails = /\/pull-requests\/[^/]+\/?$/.test(location.pathname);
  const validId = /^repo_[A-Za-z0-9_-]+$/.test(projectId);
  const overview = useQuery({
    queryKey: ["repository-overview", user?.id, token, projectId],
    queryFn: ({ signal }) => repositoryService.getOverview(projectId, signal),
    enabled: validId && Boolean(user && token),
    retry: false,
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const data = overview.data;
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
  const settingsDenied =
    location.pathname.endsWith("/settings") &&
    data &&
    !data.permissions.can_view_settings;
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
          overview={overview.isError ? undefined : data}
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
                {!overview.isError && data
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
            {!validId || overview.isError ? (
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
                    onClick={() => void overview.refetch()}
                  >
                    Try again
                  </Button>
                )}
              </section>
            ) : overview.isPending || !data ? (
              <p
                role="status"
                aria-busy="true"
                className="text-muted-foreground"
              >
                Loading repository workspace...
              </p>
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
                {settingsDenied ? (
                  <section role="alert" className="rounded-xl border p-6">
                    Repository administration permission has not been verified.
                  </section>
                ) : (
                  <Outlet context={data} />
                )}
              </>
            )}
          </main>
        </SidebarInset>
      </SidebarProvider>
    </div>
  );
}
