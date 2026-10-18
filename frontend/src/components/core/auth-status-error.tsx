import { useAuth } from "@/contexts/auth-context";
import { ErrorState } from "@/components/core/feedback/error-state";
import { Button } from "@/components/ui/button";
import { useNavigate } from "react-router";
import { useGitHubAuth } from "@/hooks/use-github-auth";

export function AuthStatusError() {
  const github = useGitHubAuth();
  const { statusError, refreshStatus, isRefreshing, logout } = useAuth();
  const navigate = useNavigate();
  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-4 p-6">
      <ErrorState
        text="Unable to verify GitHub access"
        subtext={statusError ?? undefined}
      />
      <div className="flex gap-3">
        <Button onClick={() => void refreshStatus()} disabled={isRefreshing}>
          Try again
        </Button>
        <Button
          variant="outline"
          disabled={github.pending}
          onClick={() => github.start()}
        >
          Reconnect GitHub
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            logout();
            navigate("/auth/login", { replace: true });
          }}
        >
          Sign out
        </Button>
      </div>
      {github.error && <p role="alert">{github.error}</p>}
    </div>
  );
}
