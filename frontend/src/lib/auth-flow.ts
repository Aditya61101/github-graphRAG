import { jwtDecode } from "jwt-decode";
import type { AuthStatus, OnboardingCompletion, User } from "@/types/auth";

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
  return status.onboarding_completed === true ? "/projects" : "/install";
}

export function mergeOnboardingCompletion(
  status: AuthStatus,
  result: OnboardingCompletion
): AuthStatus {
  if (
    result.onboarding_completed !== true ||
    !result.onboarding_completed_at ||
    result.next !== "/projects"
  ) {
    throw new Error("Onboarding has not been confirmed. Please try again.");
  }
  return {
    ...status,
    onboarding_completed: true,
    onboarding_completed_at: result.onboarding_completed_at,
    next: "/projects",
    user: {
      ...status.user,
      onboarding_completed_at: result.onboarding_completed_at,
    },
    installation_onboarding_required: false,
  };
}

// Never navigate to an arbitrary URL supplied in the callback query string.
export function getCallbackDestination(
  next: string | null
): "/install" | "/projects" {
  return next === "/install" ? "/install" : "/projects";
}
