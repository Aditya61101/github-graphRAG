import type { RouteObject } from "react-router";

import AuthSuccess from "@/features/auth/auth-success";
import LoginPage from "@/features/auth/login";

export const authRoutes: RouteObject[] = [
  {
    path: "/auth",
    children: [
      {
        path: "login",
        element: <LoginPage />,
      },
      {
        path: "success",
        element: <AuthSuccess />,
      },
    ],
  },
];
