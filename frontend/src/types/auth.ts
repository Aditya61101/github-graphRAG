export type User = {
  id: string;
  username: string;
  email: string | null;
  avatarUrl: string | undefined;
};

export type InstallationSummary = {
  id: string;
  account_login: string;
  account_type: string;
  status: string;
  repository_selection: string;
  public_repository_count: number;
};

export type AuthStatus = {
  user: {
    id: string;
    username: string;
    email: string | null;
    avatar_url: string | null;
    onboarding_completed_at: string | null;
  };
  installations: InstallationSummary[];
  installation_onboarding_required: boolean;
  reconnect_required: boolean;
  onboarding_completed: boolean;
  onboarding_completed_at: string | null;
  next: "/install" | "/projects";
  installation_discovery_status: "not_requested";
};

export type InstallationOutcome =
  | "success"
  | "cancelled"
  | "pending_approval"
  | "no_eligible_repositories"
  | "temporary_failure"
  | "access_denied"
  | "reauthorization_required"
  | "authorization_failed";

export type OnboardingCompletion = Pick<
  AuthStatus,
  "onboarding_completed" | "onboarding_completed_at" | "next"
>;

export type OnboardingRepository = {
  github_repository_id: string;
  installation_id: string;
  full_name: string;
  repository_url: string;
  name: string;
  is_private: false;
};

export type OnboardingStatus = OnboardingCompletion & {
  eligible: boolean;
  screen: "completed" | "available_repositories" | "install";
  installation_discovery_status: "complete";
  installation_outcome: InstallationOutcome | null;
  installations: (InstallationSummary & {
    repositories: OnboardingRepository[];
  })[];
};

export type InstallationVerification = OnboardingCompletion & {
  installation_outcome: InstallationOutcome;
};
