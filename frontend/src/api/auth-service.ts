import { apiClient } from "@/lib/api-client";
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
  startInstallation: async (): Promise<string> => {
    const response = await apiClient.post<{ installation_url: string }>(
      "/auth/github/install",
      undefined,
      { withCredentials: true }
    );
    return response.data.installation_url;
  },
  loginWithGithub: () => {
    window.location.assign(
      `${import.meta.env.VITE_BACKEND_URL}/auth/github/login`
    );
  },
};
