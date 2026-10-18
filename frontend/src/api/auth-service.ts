import { apiClient } from "@/lib/api-client";
import type { InstallationHandoff } from "@/lib/auth-popup";
import type {
  AuthStatus,
  InstallationVerification,
  OnboardingCompletion,
  OnboardingStatus,
} from "@/types/auth";

export const authService = {
  getStatus: async (signal?: AbortSignal): Promise<AuthStatus> => {
    const response = await apiClient.get<AuthStatus>("/auth/me", { signal });
    return response.data;
  },
  getOnboarding: async (signal?: AbortSignal): Promise<OnboardingStatus> => {
    const response = await apiClient.get<OnboardingStatus>("/auth/onboarding", {
      signal,
      withCredentials: true,
    });
    return response.data;
  },
  completeOnboarding: async (): Promise<OnboardingCompletion> => {
    const response = await apiClient.post<OnboardingCompletion>(
      "/auth/onboarding/complete"
    );
    return response.data;
  },
  verifyInstallation: async (): Promise<InstallationVerification> => {
    const response = await apiClient.post<InstallationVerification>(
      "/auth/onboarding/installation/verify",
      undefined,
      { withCredentials: true }
    );
    return response.data;
  },
  startInstallation: async (): Promise<InstallationHandoff> => {
    const response = await apiClient.post<InstallationHandoff>(
      "/auth/github/install",
      undefined,
      { headers: { "X-DecisionGuard-Auth-Flow": "post-handoff-v1" } }
    );
    return response.data;
  },
};
