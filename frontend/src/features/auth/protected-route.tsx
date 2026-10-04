import { Navigate, Outlet } from "react-router";

import { useAuth } from "@/components/providers/auth-provider";
import { LoadingScreen } from "@/components/loading-screen";

export default function ProtectedRoute() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return <LoadingScreen />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/auth/sign-in" replace />;
  }

  return <Outlet />;
}
