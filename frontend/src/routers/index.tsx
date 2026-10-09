import { createBrowserRouter, Navigate } from "react-router";

import { authRoutes } from "@/routers/auth-routes";
import ProtectedRouter from "@/components/core/protected-route";
import DashboardLayout from "@/layouts/dashboard-layout";
import { NotFound } from "@/pages/not-found";
import ProjectsPage from "@/pages/projects";
import ProjectDetailsPage from "@/pages/project-details";
import ExplorePage from "@/pages/explore";
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
      {
        path: "/",
        element: <Navigate to="/projects" replace />,
      },
      {
        element: <DashboardLayout />,
        handle: { breadcrumb: "Projects" },
        children: [
          {
            path: "/projects",
            element: <ProjectsPage />,
            handle: { breadcrumb: "Browse Projects" },
          },
          {
            path: "/projects/:projectId",
            element: <ProjectDetailsPage />,
            handle: { breadcrumb: "Project Details" },
          },
          {
            path: "/projects/:projectId/explore",
            element: <ExplorePage />,
            handle: { breadcrumb: "Explore" },
          },
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
