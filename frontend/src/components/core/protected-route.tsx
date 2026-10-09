import { Navigate, Outlet } from "react-router";

import { useAuth } from "@/contexts/auth-context";
import { LoadingScreen } from "@/components/core/loading-screen";
import { AuthStatusError } from "@/components/core/auth-status-error";
import { getAuthDestination } from "@/lib/auth-flow";

export default function ProtectedRouter({
  requireInstallation = true,
}: {
  requireInstallation?: boolean;
}) {
  const { isAuthenticated, isLoading, statusError, status } = useAuth();

  if (isLoading) {
    return <LoadingScreen />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/auth/login" replace />;
  }

  if (statusError) return <AuthStatusError />;
  if (!status) return <LoadingScreen />;
  if (requireInstallation && getAuthDestination(status) === "/install") {
    return <Navigate to="/install" replace />;
  }

  return <Outlet />;
}
