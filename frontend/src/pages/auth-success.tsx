import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router";
import { useAuth } from "@/contexts/auth-context";
import { getAuthDestination } from "@/lib/auth-flow";
import { parseInstallationOutcome } from "@/lib/onboarding-flow";
import { LoadingScreen } from "@/components/core/loading-screen";
import { AuthStatusError } from "@/components/core/auth-status-error";

export default function AuthSuccess() {
  const navigate = useNavigate();
  const location = useLocation();
  const { token, status, isLoading, isRefreshing, statusError } = useAuth();
  const outcome = parseInstallationOutcome(location.state?.installationOutcome);
  useEffect(() => {
    // Legacy token URLs are discarded without reading or accepting credentials.
    window.history.replaceState(
      window.history.state,
      "",
      window.location.pathname
    );
    if (!token || location.state?.popupCompletion !== true) {
      navigate("/auth/login", { replace: true });
      return;
    }
    if (isLoading || isRefreshing || statusError || !status) return;
    const destination =
      outcome && outcome !== "success"
        ? "/install"
        : getAuthDestination(status);
    navigate(destination, {
      replace: true,
      state:
        destination === "/install" ? { installationOutcome: outcome } : null,
    });
  }, [
    token,
    status,
    isLoading,
    isRefreshing,
    statusError,
    outcome,
    location.state,
    navigate,
  ]);
  if (token && statusError) return <AuthStatusError />;
  return <LoadingScreen loadingText="Verifying your GitHub access..." />;
}
