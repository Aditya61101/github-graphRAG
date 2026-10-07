export type RepositoryDto = {
  github_repository_id: string;
  owner: string;
  name: string;
  full_name: string;
  repository_url: string;
  default_branch: string;
  tracked_branch: string;
  is_private: boolean;
  description: string;
  status: string;
  tracked_repository_id: string | null;
  indexed_commit_sha: string | null;
  updated_at: string;
};

export type Repository = {
  id: string;
  name: string;
  description: string;
  isPrivate: boolean;
  updatedAt: string;
};
