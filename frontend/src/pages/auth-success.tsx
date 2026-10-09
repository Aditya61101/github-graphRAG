import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { useAuth } from "@/contexts/auth-context";
import { getAuthDestination, getCallbackDestination } from "@/lib/auth-flow";
import { LoadingScreen } from "@/components/core/loading-screen";
import { AuthStatusError } from "@/components/core/auth-status-error";

export default function AuthSuccess() {
  const navigate = useNavigate();
  const { login, logout, token, status, isLoading, statusError } = useAuth();
  const [callback] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    return {
      token: params.get("token"),
      destination: getCallbackDestination(params.get("next")),
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
    const destination = getAuthDestination(status);
    // Current verified access takes precedence over the initial callback hint.
    navigate(
      destination === callback.destination ? callback.destination : destination,
      { replace: true }
    );
  }, [callback, token, status, isLoading, statusError, navigate]);

  if (token === callback.token && statusError) return <AuthStatusError />;
  return <LoadingScreen loadingText="Verifying your GitHub access..." />;
}
