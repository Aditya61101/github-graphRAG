import { apiClient } from "@/lib/api-client";
import type { AuthStatus } from "@/types/auth";

export const authService = {
  getStatus: async (signal?: AbortSignal): Promise<AuthStatus> => {
    const response = await apiClient.get<AuthStatus>("/auth/me", { signal });
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
