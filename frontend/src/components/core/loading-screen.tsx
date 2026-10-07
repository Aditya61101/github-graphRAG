import { Logo } from "@/components/core/logo";

type LoadingScreenProps = {
  loadingText?: string;
};

export function LoadingScreen({
  loadingText = "Loading, please wait...",
}: LoadingScreenProps) {
  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-6 bg-muted p-6 md:p-10">
      <div className="flex w-full max-w-sm flex-col gap-6">
        <Logo />
        <p className="animate-pulse text-center text-muted-foreground">
          {loadingText}
        </p>
      </div>
    </div>
  );
}
