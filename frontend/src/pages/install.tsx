import { useState } from "react";
import { Link } from "react-router";
import { Loader, RefreshCwIcon } from "lucide-react";

import { authService } from "@/api/auth-service";
import { useAuth } from "@/contexts/auth-context";
import { getApiErrorMessage } from "@/lib/api-error";
import { GithubIcon } from "@/components/icons";
import { Logo } from "@/components/core/logo";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default function InstallPage() {
  const { user, status, isRefreshing, refreshStatus, logout } = useAuth();
  const [isStarting, setIsStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reconnect = status?.reconnect_required;
  const ready =
    status && !reconnect && !status.installation_onboarding_required;

  const startInstallation = async () => {
    setError(null);
    setIsStarting(true);
    try {
      const url = await authService.startInstallation();
      window.location.assign(url);
    } catch (error) {
      setError(
        getApiErrorMessage(
          error,
          "Could not start installation. Please try again."
        )
      );
      setIsStarting(false);
    }
  };

  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-6 bg-muted p-6 md:p-10">
      <div className="flex w-full max-w-lg flex-col gap-6">
        <Logo />
        <Card>
          <CardHeader>
            <CardTitle className="text-xl">
              {reconnect
                ? "Reconnect GitHub"
                : "Connect DecisionGuard to GitHub"}
            </CardTitle>
            <CardDescription>
              Signed in as {user?.username}.{" "}
              {reconnect
                ? "Your GitHub authorization needs to be renewed before repository access can be verified."
                : "Install the GitHub App and choose which repositories to grant access to on GitHub. This prototype supports public repositories only."}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            {!reconnect && !ready && (
              <p className="text-sm text-muted-foreground">
                No usable public repository grants were found. If an
                installation is awaiting organization approval, complete that
                approval on GitHub and check access again.
              </p>
            )}
            {status && status.installations.length > 0 && (
              <ul className="space-y-2">
                {status.installations.map((installation) => (
                  <li
                    key={installation.id}
                    className="rounded-lg border p-3 text-sm"
                  >
                    <span className="font-medium">
                      {installation.account_login}
                    </span>
                    <span className="ml-2 text-muted-foreground">
                      {installation.public_repository_count} accessible public
                      repositories
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <div className="flex flex-col gap-3">
              {reconnect ? (
                <Button onClick={authService.loginWithGithub}>
                  <GithubIcon />
                  Reconnect GitHub
                </Button>
              ) : (
                <Button
                  onClick={() => void startInstallation()}
                  disabled={isStarting || isRefreshing}
                >
                  {isStarting ? (
                    <Loader className="animate-spin" />
                  ) : (
                    <GithubIcon />
                  )}
                  {isStarting
                    ? "Opening GitHub..."
                    : ready
                      ? "Manage repository access on GitHub"
                      : "Install DecisionGuard"}
                </Button>
              )}
              <Button
                variant="outline"
                onClick={() => void refreshStatus()}
                disabled={isStarting || isRefreshing}
              >
                <RefreshCwIcon
                  className={isRefreshing ? "animate-spin" : undefined}
                />
                Check access again
              </Button>
              {ready && (
                <Button variant="outline" render={<Link to="/projects" />}>
                  Continue to projects
                </Button>
              )}
              <Button variant="ghost" onClick={logout}>
                Sign out
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
