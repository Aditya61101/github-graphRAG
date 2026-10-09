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
  };
  installations: InstallationSummary[];
  installation_onboarding_required: boolean;
  reconnect_required: boolean;
};
