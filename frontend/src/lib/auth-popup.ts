import { isValidToken } from "@/lib/auth-flow";
import { jwtDecode } from "jwt-decode";
import { parseInstallationOutcome } from "@/lib/onboarding-flow";
import type { InstallationOutcome } from "@/types/auth";

export type AuthCompletion = {
  type: "decisionguard.auth";
  token: string;
  next: "/install" | "/projects";
  installation_outcome: InstallationOutcome | null;
};
export type InstallationHandoff = {
  flow: "post-handoff-v1";
  method: "POST";
  action: string;
  fields: { handoff: string };
  expires_in: number;
};

export function apiOrigin(): string {
  if (!import.meta.env.VITE_BACKEND_URL)
    throw new Error("The canonical API URL has not been configured.");
  const url = new URL(import.meta.env.VITE_BACKEND_URL, window.location.origin);
  if (url.username || url.password || url.search || url.hash)
    throw new Error("Invalid API configuration.");
  return url.origin;
}

export function validCompletion(value: unknown): value is AuthCompletion {
  if (!value || typeof value !== "object") return false;
  const data = value as Record<string, unknown>;
  return (
    data.type === "decisionguard.auth" &&
    typeof data.token === "string" &&
    isValidToken(data.token) &&
    (data.next === "/install" || data.next === "/projects") &&
    (data.installation_outcome === null ||
      parseInstallationOutcome(data.installation_outcome) !== null)
  );
}

export function validateAuthSession(
  completion: AuthCompletion,
  initiatingToken: string | null,
  currentToken: string | null,
  installation: boolean
) {
  if (currentToken !== initiatingToken)
    throw new Error("Your session changed. Please restart the connection.");
  if (
    installation &&
    (!initiatingToken ||
      !isValidToken(initiatingToken) ||
      jwtDecode<{ sub: string }>(completion.token).sub !==
        jwtDecode<{ sub: string }>(initiatingToken).sub)
  )
    throw new Error(
      "GitHub account mismatch. Reconnect using the same account."
    );
}

export function submitHandoff(
  value: InstallationHandoff,
  popup: Window,
  origin: string
) {
  const action = new URL(value.action);
  if (
    value.flow !== "post-handoff-v1" ||
    value.method !== "POST" ||
    action.protocol !== "https:" ||
    action.origin !== origin ||
    action.pathname !== "/auth/github/install/continue" ||
    action.search ||
    action.hash ||
    action.username ||
    action.password ||
    !value.fields ||
    typeof value.fields.handoff !== "string" ||
    !value.fields.handoff ||
    value.fields.handoff.length > 1024 ||
    !Number.isFinite(value.expires_in) ||
    value.expires_in <= 0 ||
    value.expires_in > 120
  )
    throw new Error(
      "Invalid or expired installation handoff. Please restart the connection."
    );
  if (popup.closed)
    throw new Error("GitHub window was closed. Please try again.");
  const form = document.createElement("form");
  form.method = "POST";
  form.action = action.href;
  form.target = popup.name;
  form.enctype = "application/x-www-form-urlencoded";
  form.hidden = true;
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "handoff";
  input.value = value.fields.handoff;
  form.append(input);
  document.body.append(form);
  try {
    form.submit();
  } finally {
    input.value = "";
    form.remove();
  }
}

// One active authentication attempt across all callers. The timer checks only
// popup lifetime; it never requests ADR status or other backend state.
let activeCancel: (() => void) | undefined;
export function openAuthPopup() {
  activeCancel?.();
  const origin = apiOrigin();
  const name = `decisionguard_auth_${crypto.randomUUID()}`;
  const popup = window.open("about:blank", name, "popup,width=600,height=760");
  if (!popup) throw new Error("Allow popups for DecisionGuard and try again.");
  let done = false;
  let rejectResult: (reason: Error) => void;
  let resolveResult: (value: AuthCompletion) => void;
  const result = new Promise<AuthCompletion>((resolve, reject) => {
    resolveResult = resolve;
    rejectResult = reject;
  });
  const cleanup = () => {
    done = true;
    window.removeEventListener("message", receive);
    window.clearInterval(closed);
    window.clearTimeout(expiry);
    if (activeCancel === cancel) activeCancel = undefined;
    popup.close();
  };
  const cancel = () => {
    if (done) return;
    cleanup();
    rejectResult(new Error("GitHub connection cancelled. Please try again."));
  };
  const receive = (event: MessageEvent) => {
    if (
      done ||
      event.origin !== origin ||
      event.source !== popup ||
      !validCompletion(event.data)
    )
      return;
    const completion = event.data;
    cleanup();
    resolveResult(completion);
  };
  const closed = window.setInterval(() => {
    if (popup.closed) cancel();
  }, 500);
  const expiry = window.setTimeout(() => {
    if (done) return;
    cleanup();
    rejectResult(new Error("GitHub connection expired. Please start again."));
  }, 600_000);
  activeCancel = cancel;
  window.addEventListener("message", receive);
  return { popup, origin, result, cancel };
}
