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
import { isValidToken, mapAuthUser } from "@/lib/auth-flow";
import { getApiErrorMessage } from "@/lib/api-error";
import type { AuthStatus, User } from "@/types/auth";

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
};

const AuthContext = createContext<AuthContextType | null>(null);

function restoreToken(): string | null {
  // The callback will replace the token. Do not send a stale account's token
  // to /auth/me before that effect runs (an old 401 could interrupt login).
  if (window.location.pathname === "/auth/success") return null;
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
    }),
    [token, status, error, isPending, isFetching, login, logout, refreshStatus]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}
