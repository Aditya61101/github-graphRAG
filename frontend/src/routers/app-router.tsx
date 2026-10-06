import { createBrowserRouter, Navigate } from "react-router";

import { authRoutes } from "@/routers/auth-routes";
import ProtectedRouter from "@/routers/components/protected-route";
import DashboardLayout from "@/features/dashboard/dashboard-layout";
import RepositoriesPage from "@/features/dashboard/pages/repositories";
import { NotFound } from "@/features/errors/not-found";

export const appRouter = createBrowserRouter([
  ...authRoutes,
  {
    element: <ProtectedRouter />,
    children: [
      {
        path: "/",
        element: <Navigate to="/repositories" replace />,
      },
      {
        element: <DashboardLayout />,
        handle: { breadcrumb: "Repositories" },
        children: [
          {
            path: "/repositories",
            element: <RepositoriesPage />,
            handle: { breadcrumb: "Browse repositories" },
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
