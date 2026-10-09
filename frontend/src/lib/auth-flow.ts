import { jwtDecode } from "jwt-decode";
import type { AuthStatus, User } from "@/types/auth";

export function isValidToken(token: string): boolean {
  try {
    const payload = jwtDecode<{ sub?: string; exp?: number }>(token);
    return (
      typeof payload.sub === "string" &&
      Boolean(payload.sub) &&
      typeof payload.exp === "number" &&
      payload.exp * 1000 > Date.now()
    );
  } catch {
    return false;
  }
}

export function mapAuthUser(status: AuthStatus): User {
  return {
    id: status.user.id,
    username: status.user.username,
    email: status.user.email,
    avatarUrl: status.user.avatar_url ?? undefined,
  };
}

export function getAuthDestination(
  status: AuthStatus
): "/install" | "/projects" {
  return status.reconnect_required || status.installation_onboarding_required
    ? "/install"
    : "/projects";
}

// Never navigate to an arbitrary URL supplied in the callback query string.
export function getCallbackDestination(
  next: string | null
): "/install" | "/projects" {
  return next === "/install" ? "/install" : "/projects";
}
