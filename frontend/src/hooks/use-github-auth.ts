import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { useAuth } from "@/contexts/auth-context";
import { authService } from "@/api/auth-service";
import {
  openAuthPopup,
  submitHandoff,
  validateAuthSession,
} from "@/lib/auth-popup";
import { getOnboardingError } from "@/lib/onboarding-flow";

export function useGitHubAuth() {
  const { login, token } = useAuth();
  const navigate = useNavigate();
  const attempt = useRef<ReturnType<typeof openAuthPopup> | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(
    () => () => {
      const current = attempt.current;
      attempt.current = null;
      current?.cancel();
    },
    [token]
  );

  const start = (installation = false) => {
    if (attempt.current) return;
    setError(null);
    let flow: ReturnType<typeof openAuthPopup>;
    try {
      flow = openAuthPopup();
    } catch (failure) {
      setError(getOnboardingError(failure).message);
      return;
    }
    attempt.current = flow;
    setPending(true);
    const initiatingToken = localStorage.getItem("token");
    // Attach rejection handling before initiation can fail or be cancelled.
    void flow.result
      .then((completion) => {
        if (attempt.current !== flow) return;
        validateAuthSession(
          completion,
          initiatingToken,
          localStorage.getItem("token"),
          installation
        );
        if (!login(completion.token))
          throw new Error(
            "Invalid authentication response. Please sign in again."
          );
        navigate("/auth/success", {
          replace: true,
          state: {
            popupCompletion: true,
            installationOutcome: completion.installation_outcome,
          },
        });
      })
      .catch((failure: unknown) => {
        if (attempt.current === flow)
          setError(getOnboardingError(failure).message);
      })
      .finally(() => {
        if (attempt.current === flow) {
          attempt.current = null;
          setPending(false);
        }
      });
    if (installation) {
      const requestedAt = performance.now();
      void authService
        .startInstallation()
        .then((handoff) => {
          if (attempt.current !== flow) return;
          if (performance.now() - requestedAt >= handoff.expires_in * 1000)
            throw new Error(
              "Installation handoff expired. Please start again."
            );
          if (localStorage.getItem("token") !== initiatingToken)
            throw new Error(
              "Your session changed. Please restart the connection."
            );
          submitHandoff(handoff, flow.popup, flow.origin);
        })
        .catch((failure: unknown) => {
          if (attempt.current !== flow) return;
          attempt.current = null;
          flow.cancel();
          setPending(false);
          setError(getOnboardingError(failure).message);
        });
    } else {
      flow.popup.location.replace(`${flow.origin}/auth/github/login`);
    }
  };
  return { start, pending, error, cancel: () => attempt.current?.cancel() };
}
