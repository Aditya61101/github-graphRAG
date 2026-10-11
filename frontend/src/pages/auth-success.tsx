import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { useAuth } from "@/contexts/auth-context";
import { getAuthDestination } from "@/lib/auth-flow";
import { parseInstallationOutcome } from "@/lib/onboarding-flow";
import { LoadingScreen } from "@/components/core/loading-screen";
import { AuthStatusError } from "@/components/core/auth-status-error";

export default function AuthSuccess() {
  const navigate = useNavigate();
  const { login, logout, token, status, isLoading, statusError } = useAuth();
  const [callback] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    return {
      token: params.get("token"),
      installationOutcome: parseInstallationOutcome(
        params.get("installation_outcome")
      ),
    };
  });

  useEffect(() => {
    // Consume the callback once; do not leave the bearer token in history.
    window.history.replaceState(
      window.history.state,
      "",
      window.location.pathname
    );
    if (!callback.token || !login(callback.token)) {
      logout();
      navigate("/auth/login", { replace: true });
    }
  }, [callback, login, logout, navigate]);

  useEffect(() => {
    if (token !== callback.token || isLoading || statusError || !status) return;
    // An unsuccessful installation attempt needs a recoverable access screen,
    // including for completed users. This hint never changes completion.
    const needsRecovery =
      callback.installationOutcome !== null &&
      callback.installationOutcome !== "success";
    const destination = needsRecovery ? "/install" : getAuthDestination(status);
    // Persisted completion from /auth/me is authoritative. Callback parameters
    // cannot complete onboarding or choose an arbitrary destination.
    navigate(destination, {
      replace: true,
      state:
        destination === "/install"
          ? { installationOutcome: callback.installationOutcome }
          : null,
    });
  }, [callback, token, status, isLoading, statusError, navigate]);

  if (token === callback.token && statusError) return <AuthStatusError />;
  return <LoadingScreen loadingText="Verifying your GitHub access..." />;
}
