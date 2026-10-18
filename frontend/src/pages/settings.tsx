import { Link, useNavigate } from "react-router";
import { ExternalLink, LogOut, ShieldCheck, TriangleAlert } from "lucide-react";
import { AppHeader } from "@/components/core/app-header";
import { GithubIcon } from "@/components/icons";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/auth-context";
import { useGitHubAuth } from "@/hooks/use-github-auth";

export default function SettingsPage() {
  const { user, status, logout } = useAuth();
  const github = useGitHubAuth();
  const navigate = useNavigate();
  const username = user?.username ?? "GitHub user";
  const reconnect = status?.reconnect_required === true;
  return (
    <>
      <AppHeader
        crumbs={[
          { label: "All Repositories", pathname: "/projects" },
          { label: "My Profile", pathname: "/settings" },
        ]}
      />
      <main className="mx-auto w-full max-w-3xl space-y-6 p-4 sm:p-6">
        <header className="space-y-2">
          <h1 className="text-3xl font-semibold tracking-tight">My Profile</h1>
          <p className="text-sm text-muted-foreground">
            Your identity is supplied by GitHub. Manage repository access
            through your GitHub connection.
          </p>
        </header>
        <section
          aria-label="GitHub profile"
          className="space-y-6 rounded-xl border bg-card p-5 sm:p-6"
        >
          <div className="flex min-w-0 items-center gap-4">
            <Avatar className="size-16 shrink-0 border">
              <AvatarImage
                src={user?.avatarUrl}
                alt={`${username}'s GitHub avatar`}
              />
              <AvatarFallback className="bg-muted text-xl">
                {username.charAt(0).toUpperCase()}
              </AvatarFallback>
            </Avatar>
            <div className="min-w-0 space-y-1">
              <h2 className="text-xl font-semibold break-all">{username}</h2>
              <p className="text-sm text-muted-foreground">
                Authenticated GitHub account
              </p>
            </div>
          </div>
          <dl className="grid gap-4 border-t pt-5 sm:grid-cols-2">
            <div className="space-y-1">
              <dt className="text-sm text-muted-foreground">GitHub username</dt>
              <dd className="font-medium break-all">{username}</dd>
            </div>
            <div className="space-y-1">
              <dt className="text-sm text-muted-foreground">Email address</dt>
              <dd className="font-medium break-all">
                {user?.email?.trim() || "Not provided by GitHub"}
              </dd>
            </div>
          </dl>
          {user?.username && (
            <Button
              nativeButton={false}
              variant="outline"
              render={
                <a
                  href={`https://github.com/${encodeURIComponent(user.username)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                />
              }
            >
              <GithubIcon aria-hidden="true" /> View GitHub account{" "}
              <ExternalLink aria-hidden="true" />
            </Button>
          )}
        </section>
        <section
          aria-labelledby="github-connection-title"
          className="space-y-4 rounded-xl border bg-card p-5 sm:p-6"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="github-connection-title" className="font-semibold">
              GitHub connection
            </h2>
            <span
              className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-sm ${reconnect ? "text-amber-700 dark:text-amber-300" : "text-foreground"}`}
            >
              {reconnect ? (
                <TriangleAlert aria-hidden="true" className="size-4" />
              ) : (
                <ShieldCheck aria-hidden="true" className="size-4" />
              )}
              {reconnect ? "Reconnection required" : "Connected"}
            </span>
          </div>
          <p className="text-sm text-muted-foreground">
            {reconnect
              ? "Renew your GitHub authorization to check repository access. Your DecisionGuard account remains signed in."
              : "Your GitHub authorization is connected. Repository grants are managed separately through the GitHub App."}
          </p>
          <div className="flex flex-wrap gap-3">
            {reconnect && (
              <Button disabled={github.pending} onClick={() => github.start()}>
                {github.pending ? "Waiting for GitHub..." : "Reconnect GitHub"}
              </Button>
            )}
            <Button
              nativeButton={false}
              variant="outline"
              render={<Link to="/install" />}
            >
              Manage repository access
            </Button>
          </div>
          {github.error && (
            <p role="alert" className="text-sm text-destructive">
              {github.error}
            </p>
          )}
        </section>
        <footer className="flex flex-wrap items-center justify-between gap-3">
          <Button
            nativeButton={false}
            variant="ghost"
            render={<Link to="/projects" />}
          >
            All Repositories
          </Button>
          <Button
            variant="outline"
            onClick={() => {
              logout();
              navigate("/auth/login", { replace: true });
            }}
          >
            <LogOut aria-hidden="true" /> Log out
          </Button>
        </footer>
      </main>
    </>
  );
}
