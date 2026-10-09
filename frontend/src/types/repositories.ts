export type RepositoryDto = {
  id: string | null;
  installation_id: string;
  github_repository_id: string;
  owner: string;
  name: string;
  full_name: string;
  repository_url: string;
  default_branch: string;
  tracked_branch: string | null;
  is_private: boolean;
  description: string | null;
  status: string;
  tracked_repository_id: string | null;
  indexed_commit_sha: string | null;
  updated_at: string | null;
};

export type Repository = {
  // Picker ID is a GitHub ID, not a backend repo_... identifier.
  id: string;
  githubRepositoryId: string;
  trackedRepositoryId: string | null;
  installationId: string;
  fullName: string;
  repositoryUrl: string;
  defaultBranch: string;
  trackedBranch: string | null;
  status: string;
  indexedCommitSha: string | null;
  name: string;
  description: string | null;
  isPrivate: boolean;
  updatedAt: string | null;
};
