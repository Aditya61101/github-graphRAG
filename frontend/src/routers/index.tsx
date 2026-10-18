import { createBrowserRouter, Navigate } from "react-router";
import { LazyPage } from "@/components/core/lazy-page";
import { lazy } from "react";

import { authRoutes } from "@/routers/auth-routes";
import ProtectedRouter from "@/components/core/protected-route";
import DashboardLayout from "@/layouts/dashboard-layout";
import { NotFound } from "@/pages/not-found";
import ProjectsPage from "@/pages/projects";
import RepositoryLayout from "@/layouts/repository-layout";
import RepositoryOverviewPage from "@/pages/repository-overview";
const RepositoryDecisionsPage = lazy(
  () => import("@/pages/repository-decisions")
);
const RepositoryPullRequestsPage = lazy(
  () => import("@/pages/repository-pull-requests")
);
const RepositoryPullRequestDetailPage = lazy(
  () => import("@/pages/repository-pull-request-detail")
);
const ExplorePage = lazy(() => import("@/pages/explore"));
const SettingsPage = lazy(() => import("@/pages/settings"));
const InstallPage = lazy(() => import("@/pages/install"));

export const appRouter = createBrowserRouter([
  ...authRoutes,
  {
    element: <ProtectedRouter requireInstallation={false} />,
    children: [
      {
        path: "/install",
        element: (
          <LazyPage label="Loading repository access...">
            <InstallPage />
          </LazyPage>
        ),
      },
    ],
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
          {
            path: "decisions",
            element: (
              <LazyPage label="Loading Decisions...">
                <RepositoryDecisionsPage />
              </LazyPage>
            ),
          },
          {
            path: "explore",
            element: (
              <LazyPage label="Loading Knowledge Explorer...">
                <ExplorePage />
              </LazyPage>
            ),
          },
          {
            path: "pull-requests",
            element: (
              <LazyPage label="Loading pull requests...">
                <RepositoryPullRequestsPage />
              </LazyPage>
            ),
          },
          {
            path: "pull-requests/:prNumber",
            element: (
              <LazyPage label="Loading pull request review...">
                <RepositoryPullRequestDetailPage />
              </LazyPage>
            ),
          },
          { path: "settings", element: <Navigate to="../overview" replace /> },
        ],
      },
      {
        element: <DashboardLayout />,
        handle: { breadcrumb: "Projects" },
        children: [
          {
            path: "/settings",
            element: (
              <LazyPage label="Loading My Profile...">
                <SettingsPage />
              </LazyPage>
            ),
            handle: { breadcrumb: "My Profile" },
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
