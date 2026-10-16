import { createBrowserRouter, Navigate } from "react-router";
import { lazy, Suspense } from "react";

import { authRoutes } from "@/routers/auth-routes";
import ProtectedRouter from "@/components/core/protected-route";
import DashboardLayout from "@/layouts/dashboard-layout";
import { NotFound } from "@/pages/not-found";
import ProjectsPage from "@/pages/projects";
import RepositoryLayout from "@/layouts/repository-layout";
import RepositoryOverviewPage from "@/pages/repository-overview";
import RepositoryComingLater from "@/pages/repository-coming-later";
import RepositoryDecisionsPage from "@/pages/repository-decisions";
import RepositoryPullRequestsPage from "@/pages/repository-pull-requests";
import RepositoryPullRequestDetailPage from "@/pages/repository-pull-request-detail";
const ExplorePage = lazy(() => import("@/pages/explore"));
import SettingsPage from "@/pages/settings";
import InstallPage from "@/pages/install";

export const appRouter = createBrowserRouter([
  ...authRoutes,
  {
    element: <ProtectedRouter requireInstallation={false} />,
    children: [{ path: "/install", element: <InstallPage /> }],
  },
  {
    element: <ProtectedRouter />,
    children: [
      { path: "/projects", element: <ProjectsPage /> },
      {
        path: "/",
        element: <Navigate to="/projects" replace />,
      },
      {
        path: "/projects/:projectId",
        element: <RepositoryLayout />,
        children: [
          { index: true, element: <Navigate to="overview" replace /> },
          { path: "overview", element: <RepositoryOverviewPage /> },
          { path: "decisions", element: <RepositoryDecisionsPage /> },
          {
            path: "explore",
            element: (
              <Suspense
                fallback={<p role="status">Loading Knowledge Explorer...</p>}
              >
                <ExplorePage />
              </Suspense>
            ),
          },
          { path: "pull-requests", element: <RepositoryPullRequestsPage /> },
          {
            path: "pull-requests/:prNumber",
            element: <RepositoryPullRequestDetailPage />,
          },
          { path: "settings", element: <RepositoryComingLater /> },
        ],
      },
      {
        element: <DashboardLayout />,
        handle: { breadcrumb: "Projects" },
        children: [
          {
            path: "/settings",
            element: <SettingsPage />,
            handle: { breadcrumb: "Settings" },
          },
        ],
      },
    ],
  },
  {
    path: "*",
    element: <NotFound />,
  },
]);
