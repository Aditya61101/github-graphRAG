import axios from "axios";
import { getApiErrorMessage } from "@/lib/api-error";
import type { InstallationOutcome } from "@/types/auth";

const outcomeMessages: Record<InstallationOutcome, string> = {
  success: "GitHub access was verified. Finishing onboarding...",
  cancelled:
    "GitHub installation was cancelled. You can try again or continue with repositories already available to you.",
  pending_approval:
    "Your installation is awaiting organization approval. Ask an organization owner to approve it, then check access again.",
  no_eligible_repositories:
    "No accessible public repositories were granted by this installation. Choose at least one public repository you can access on GitHub, then try again.",
  temporary_failure:
    "GitHub is temporarily unavailable. Your installation could not be verified. Try verification again or restart the GitHub connection.",
  access_denied:
    "This installation is not accessible to your GitHub account. Check your repository grants or connect again using the same account.",
  reauthorization_required:
    "Your GitHub authorization needs to be renewed. Reconnect GitHub to verify repository access.",
  authorization_failed:
    "GitHub authorization could not be completed. Restart the connection and try again.",
};

export function parseInstallationOutcome(
  value: unknown
): InstallationOutcome | null {
  return typeof value === "string" && Object.hasOwn(outcomeMessages, value)
    ? (value as InstallationOutcome)
    : null;
}

export function getInstallationMessage(outcome: InstallationOutcome): string {
  return outcomeMessages[outcome];
}

export function getOnboardingError(error: unknown): {
  message: string;
  outcome: InstallationOutcome | null;
} {
  if (error instanceof Error && !axios.isAxiosError(error)) {
    return { outcome: null, message: error.message };
  }
  if (
    axios.isAxiosError<{
      detail?: { outcome?: unknown; message?: unknown } | string;
    }>(error)
  ) {
    const detail = error.response?.data.detail;
    if (detail && typeof detail === "object") {
      const outcome = parseInstallationOutcome(detail.outcome);
      return {
        outcome,
        message:
          typeof detail.message === "string"
            ? detail.message
            : outcome
              ? getInstallationMessage(outcome)
              : "Could not verify GitHub access. Please try again.",
      };
    }
  }
  return {
    outcome: null,
    message: getApiErrorMessage(
      error,
      "Could not verify GitHub access. Please try again."
    ),
  };
}
