import type { RouteObject } from "react-router";

import AuthSuccess from "@/pages/auth-success";
import LoginPage from "@/pages/login";

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
