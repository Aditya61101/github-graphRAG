import { Loader, RefreshCwIcon } from "lucide-react";
import { GithubIcon } from "@/components/icons";
import { Logo } from "@/components/core/logo";
import { ErrorState } from "@/components/core/feedback/error-state";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { RepositoryGroups } from "@/components/onboarding/repository-groups";
import { getInstallationMessage } from "@/lib/onboarding-flow";
import type { InstallationOutcome, OnboardingStatus } from "@/types/auth";

type OnboardingScreenProps = {
  manageAccess?: boolean;
  onReturnProjects?: () => void;
  username?: string;
  data?: OnboardingStatus;
  isLoading: boolean;
  isRefreshing: boolean;
  busy: boolean;
  action: "install" | "complete" | "verify" | null;
  discoveryError: string | null;
  actionError: string | null;
  outcome: InstallationOutcome | null;
  reconnect: boolean;
  onInstall: () => void;
  onComplete: () => void;
  onVerify: () => void;
  onRefresh: () => void;
  onReconnect: () => void;
  onLogout: () => void;
};

export function OnboardingScreen({
  manageAccess = false,
  onReturnProjects,
  username,
  data,
  isLoading,
  isRefreshing,
  busy,
  action,
  discoveryError,
  actionError,
  outcome,
  reconnect,
  onInstall,
  onComplete,
  onVerify,
  onRefresh,
  onReconnect,
  onLogout,
}: OnboardingScreenProps) {
  const ready = !isLoading && !discoveryError && data !== undefined;
  const available = ready && data.eligible;
  const canVerify =
    outcome === "temporary_failure" ||
    outcome === "no_eligible_repositories" ||
    outcome === "access_denied";

  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-6 bg-muted p-6 md:p-10">
      <div className="flex w-full max-w-lg flex-col gap-6">
        <Logo />
        <Card>
          <CardHeader>
            <CardTitle>
              <h1 className="text-xl">
                {manageAccess
                  ? "Manage GitHub repository access"
                  : isLoading
                    ? "Discovering your repositories"
                    : discoveryError
                      ? "Verify your GitHub access"
                      : available
                        ? "Available repositories to protect"
                        : "Connect DecisionGuard to GitHub"}
              </h1>
            </CardTitle>
            <CardDescription>
              Signed in as {username}.{" "}
              {manageAccess
                ? "Manage the public repositories available to DecisionGuard on GitHub. Your onboarding is already complete."
                : available
                  ? "These public repositories are accessible to your GitHub account through DecisionGuard."
                  : "DecisionGuard needs to be installed on your GitHub account or organization with access to at least one public repository."}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            {outcome && outcome !== "success" && (
              <p
                role="status"
                className="rounded-lg border bg-muted/50 p-3 text-sm text-muted-foreground"
              >
                {getInstallationMessage(outcome)}
              </p>
            )}
            {isLoading ? (
              <div
                role="status"
                className="flex items-center gap-2 text-sm text-muted-foreground"
              >
                <Loader aria-hidden="true" className="size-4 animate-spin" />
                Checking repositories accessible to your GitHub account...
              </div>
            ) : discoveryError ? (
              <div role="alert">
                <ErrorState
                  text="Unable to discover repositories"
                  subtext={discoveryError}
                />
              </div>
            ) : available ? (
              <RepositoryGroups installations={data.installations} />
            ) : ready ? (
              <p className="text-sm text-muted-foreground">
                No accessible public repositories were found. Install
                DecisionGuard on GitHub and choose the repositories you want to
                connect.
              </p>
            ) : null}
            {actionError && (
              <p role="alert" className="text-sm text-destructive">
                {actionError}
              </p>
            )}
            <div className="flex flex-col gap-3">
              {reconnect ? (
                <Button onClick={onReconnect} disabled={busy}>
                  <GithubIcon /> Reconnect GitHub
                </Button>
              ) : ready ? (
                <>
                  {available && !manageAccess && (
                    <Button
                      onClick={onComplete}
                      disabled={busy || isRefreshing}
                    >
                      {action === "complete" && (
                        <Loader aria-hidden="true" className="animate-spin" />
                      )}
                      {action === "complete"
                        ? "Confirming onboarding..."
                        : "Continue with available repositories"}
                    </Button>
                  )}
                  <Button
                    variant={available ? "outline" : "default"}
                    onClick={onInstall}
                    disabled={busy || isRefreshing}
                  >
                    {action === "install" ? (
                      <Loader aria-hidden="true" className="animate-spin" />
                    ) : (
                      <GithubIcon />
                    )}
                    {action === "install"
                      ? "Opening GitHub..."
                      : manageAccess
                        ? "Manage access on GitHub"
                        : available
                          ? "Connect more repositories"
                          : "Install DecisionGuard on GitHub"}
                  </Button>
                </>
              ) : null}
              {canVerify && !reconnect && (
                <Button
                  variant="outline"
                  onClick={onVerify}
                  disabled={busy || isRefreshing}
                >
                  {action === "verify" && (
                    <Loader aria-hidden="true" className="animate-spin" />
                  )}
                  {action === "verify"
                    ? "Verifying installation..."
                    : "Retry installation verification"}
                </Button>
              )}
              {(!available || outcome || discoveryError) && !isLoading && (
                <Button
                  variant="ghost"
                  onClick={onRefresh}
                  disabled={busy || isRefreshing}
                >
                  <RefreshCwIcon
                    aria-hidden="true"
                    className={isRefreshing ? "animate-spin" : undefined}
                  />
                  {isRefreshing ? "Checking access..." : "Check access again"}
                </Button>
              )}
              {discoveryError && !reconnect && (
                <Button
                  variant="outline"
                  onClick={onInstall}
                  disabled={busy || isRefreshing}
                >
                  <GithubIcon /> Restart GitHub connection
                </Button>
              )}
              {manageAccess && (
                <Button
                  variant="outline"
                  onClick={onReturnProjects}
                  disabled={busy}
                >
                  Back to projects
                </Button>
              )}
              <Button variant="ghost" onClick={onLogout} disabled={busy}>
                Sign out
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
