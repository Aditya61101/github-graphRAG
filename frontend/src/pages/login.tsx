import { Navigate } from "react-router";

import { authService } from "@/api/auth-service";
import { LoadingScreen } from "@/components/core/loading-screen";
import { Logo } from "@/components/core/logo";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/auth-context";
import { GithubIcon } from "@/components/icons";

const PROVIDERS = [
  {
    label: "Login with Github",
    icon: GithubIcon,
    onClick: () => authService.loginWithGithub(),
  },
] as const;

export default function LoginPage() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) return <LoadingScreen />;

  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  return (
    <div className="relative min-h-svh overflow-hidden bg-[#09070f] text-white">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_top,color-mix(in_oklab,var(--primary)_90%,transparent)_0%,#09070f_55%)]" />
      <div className="absolute inset-0 bg-[linear-gradient(to_right,rgba(255,255,255,0.04)_1px,transparent_1px)] bg-size-[28px_100%] opacity-30" />

      <div className="relative z-10 grid min-h-svh lg:grid-cols-[1fr_440px]">
        <section className="relative hidden items-center justify-center overflow-hidden px-10 py-12 lg:flex">
          <div className="absolute h-160 w-160 rounded-full border border-white/10 opacity-40 blur-3xl" />

          <div className="relative flex h-155 w-155 items-center justify-center">
            <div className="absolute inset-0 rounded-full bg-[repeating-linear-gradient(130deg,rgba(255,255,255,0.14)_0,rgba(255,255,255,0.14)_8px,transparent_8px,transparent_18px)] mask-[radial-gradient(circle,black_58%,transparent_74%)] opacity-50" />

            <div className="relative z-10 max-w-2xl text-center">
              <div className="mb-8 flex justify-center">
                <Logo />
              </div>

              <h1 className="text-5xl leading-tight font-semibold tracking-tight text-white">
                Turn scattered engineering knowledge into actionable decisions.
              </h1>

              <p className="mx-auto mt-7 max-w-lg text-lg text-zinc-400">
                Explore repositories, architectures, pull requests, ADRs, and
                code relationships through AI-powered graph intelligence built
                for modern engineering teams.
              </p>

              <div className="mt-8 flex items-center justify-center gap-3 text-sm text-zinc-300">
                <span className="rounded-full border border-white/20 bg-white/5 px-4 py-2">
                  Graph-powered RAG
                </span>

                <span className="rounded-full border border-white/20 bg-white/5 px-4 py-2">
                  Repository Intelligence
                </span>
              </div>
            </div>
          </div>
        </section>

        <section className="flex items-center justify-center bg-black/5 px-6 py-12 backdrop-blur-md sm:px-10">
          <div className="w-full max-w-md space-y-8">
            <div className="space-y-3 text-center lg:text-left">
              <div className="flex justify-center lg:hidden">
                <Logo />
              </div>

              <div>
                <h2 className="text-center text-2xl font-semibold tracking-tight text-white">
                  Sign into DecisionGuard
                </h2>
                <p className="mt-3 text-center text-base text-zinc-400">
                  Welcome back, let&apos;s start reviewing.
                </p>
              </div>
            </div>

            <div className="space-y-4">
              {PROVIDERS.map((provider) => {
                const Icon = provider.icon;

                return (
                  <Button
                    key={provider.label}
                    type="button"
                    variant="outline"
                    onClick={provider.onClick}
                    className="justify-CENTER mx-auto flex w-full max-w-xs items-center rounded-lg border border-white/10 bg-white/3 p-5 text-sm text-white transition-all hover:border-violet-400/50 hover:bg-violet-500/10 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <span className="flex items-center gap-3">
                      <Icon className="size-5" />
                      {provider.label}
                    </span>
                  </Button>
                );
              })}
            </div>

            <p className="pt-12 text-center text-xs leading-relaxed text-zinc-600">
              By continuing, you agree to the{" "}
              <button type="button" className="text-zinc-400 underline">
                Terms of Use
              </button>{" "}
              and{" "}
              <button type="button" className="text-zinc-400 underline">
                Privacy Policy
              </button>
              .
            </p>
          </div>
        </section>
      </div>
    </div>
  );
}
