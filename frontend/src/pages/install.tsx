import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { authService } from "@/api/auth-service";
import { useAuth } from "@/contexts/auth-context";
import { OnboardingScreen } from "@/components/onboarding/onboarding-screen";
import {
  getOnboardingError,
  parseInstallationOutcome,
} from "@/lib/onboarding-flow";
import type { InstallationOutcome } from "@/types/auth";

export default function InstallPage() {
  const { user, token, status, confirmOnboarding, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  // A deliberate visit after onboarding manages access; completing onboarding
  // during this visit must still send a first-time user directly to projects.
  const [manageAccess] = useState(status?.onboarding_completed === true);
  const callbackState = location.state as {
    installationOutcome?: unknown;
  } | null;
  const hint = parseInstallationOutcome(callbackState?.installationOutcome);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionOutcome, setActionOutcome] =
    useState<InstallationOutcome | null>(null);
  const discovery = useQuery({
    queryKey: ["onboarding", token],
    queryFn: async ({ signal }) => {
      const result = await authService.getOnboarding(signal);
      // Persisted completion takes precedence over repository availability.
      if (result.onboarding_completed) confirmOnboarding(result);
      return result;
    },
    enabled: Boolean(token) && (manageAccess || !status?.onboarding_completed),
    retry: false,
    staleTime: 0,
  });

  const reportError = (error: unknown) => {
    const failure = getOnboardingError(error);
    setActionError(failure.message);
    setActionOutcome(failure.outcome);
  };
  const install = useMutation({
    mutationFn: authService.startInstallation,
    onSuccess: (url) => window.location.assign(url),
    onError: reportError,
  });
  const complete = useMutation({
    mutationFn: async () => {
      const result = await authService.completeOnboarding();
      confirmOnboarding(result);
    },
    // The existing status-based Navigate below runs only after auth consumers
    // observe completion. An early imperative navigation can race the guard.
    onError: reportError,
  });
  const verify = useMutation({
    mutationFn: async () => {
      const result = await authService.verifyInstallation();
      if (result.onboarding_completed) confirmOnboarding(result);
      return result;
    },
    onSuccess: (result) => {
      setActionOutcome(result.installation_outcome);
      if (
        result.installation_outcome === "success" &&
        result.onboarding_completed
      )
        navigate("/projects", { replace: true });
    },
    onError: reportError,
  });

  if (status?.onboarding_completed && !manageAccess)
    return <Navigate to="/projects" replace />;

  const failure = discovery.error ? getOnboardingError(discovery.error) : null;
  const outcome =
    actionOutcome ??
    (discovery.data ? discovery.data.installation_outcome : hint);
  const resetAction = () => {
    setActionError(null);
    setActionOutcome(null);
  };
  return (
    <OnboardingScreen
      manageAccess={manageAccess}
      onReturnProjects={() => navigate("/projects")}
      username={user?.username}
      data={discovery.data}
      isLoading={discovery.isPending && !discovery.error}
      isRefreshing={discovery.isFetching}
      busy={install.isPending || complete.isPending || verify.isPending}
      action={
        install.isPending
          ? "install"
          : complete.isPending
            ? "complete"
            : verify.isPending
              ? "verify"
              : null
      }
      discoveryError={failure?.message ?? null}
      actionError={actionError}
      outcome={outcome}
      reconnect={
        status?.reconnect_required === true ||
        actionOutcome === "reauthorization_required" ||
        failure?.outcome === "reauthorization_required"
      }
      onInstall={() => {
        resetAction();
        install.mutate();
      }}
      onComplete={() => {
        resetAction();
        complete.mutate();
      }}
      onVerify={() => {
        resetAction();
        verify.mutate();
      }}
      onRefresh={() => {
        resetAction();
        void discovery.refetch();
      }}
      onReconnect={authService.loginWithGithub}
      onLogout={logout}
    />
  );
}
