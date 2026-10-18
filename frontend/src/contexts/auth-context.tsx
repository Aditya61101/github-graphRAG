import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { authService } from "@/api/auth-service";
import {
  isValidToken,
  mapAuthUser,
  mergeOnboardingCompletion,
} from "@/lib/auth-flow";
import { getApiErrorMessage } from "@/lib/api-error";
import type { AuthStatus, OnboardingCompletion, User } from "@/types/auth";

type AuthContextType = {
  token: string | null;
  user: User | null;
  status: AuthStatus | undefined;
  isAuthenticated: boolean;
  isLoading: boolean;
  isRefreshing: boolean;
  statusError: string | null;
  login: (token: string) => boolean;
  logout: () => void;
  refreshStatus: () => Promise<void>;
  confirmOnboarding: (result: OnboardingCompletion) => void;
};

const AuthContext = createContext<AuthContextType | null>(null);

function restoreToken(): string | null {
  const stored = localStorage.getItem("token");
  if (stored && isValidToken(stored)) return stored;
  localStorage.removeItem("token");
  return null;
}

export function AuthProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [token, setToken] = useState<string | null>(restoreToken);
  const {
    data: status,
    error,
    isPending,
    isFetching,
    refetch,
  } = useQuery({
    queryKey: ["auth", token],
    queryFn: ({ signal }) => authService.getStatus(signal),
    enabled: Boolean(token),
    retry: false,
    staleTime: 60_000,
  });

  const login = useCallback(
    (newToken: string) => {
      if (!isValidToken(newToken)) return false;
      if (localStorage.getItem("token") !== newToken) queryClient.clear();
      localStorage.setItem("token", newToken);
      setToken(newToken);
      // A popup may return the same JWT within its issuance second. Always
      // re-read persisted onboarding/account state even in that case.
      void queryClient.invalidateQueries({ queryKey: ["auth", newToken] });
      return true;
    },
    [queryClient]
  );

  const logout = useCallback(() => {
    localStorage.removeItem("token");
    queryClient.clear();
    setToken(null);
  }, [queryClient]);

  const refreshStatus = useCallback(async () => {
    await refetch();
    await queryClient.invalidateQueries({ queryKey: ["repositories"] });
  }, [refetch, queryClient]);

  const confirmOnboarding = useCallback(
    (result: OnboardingCompletion) => {
      if (!token || localStorage.getItem("token") !== token) {
        throw new Error("Your session changed. Please sign in again.");
      }
      queryClient.setQueryData<AuthStatus>(["auth", token], (current) => {
        if (!current)
          throw new Error("Your account has not been verified. Please retry.");
        return mergeOnboardingCompletion(current, result);
      });
    },
    [queryClient, token]
  );

  const value = useMemo(
    () => ({
      token,
      user: token && status ? mapAuthUser(status) : null,
      status: token ? status : undefined,
      isAuthenticated: Boolean(token),
      isLoading: Boolean(token) && isPending,
      isRefreshing: isFetching,
      statusError:
        token && error
          ? getApiErrorMessage(
              error,
              "Could not reach the backend. Please retry; your installation status has not been confirmed."
            )
          : null,
      login,
      logout,
      refreshStatus,
      confirmOnboarding,
    }),
    [
      token,
      status,
      error,
      isPending,
      isFetching,
      login,
      logout,
      refreshStatus,
      confirmOnboarding,
    ]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}
