import type { Repository, RepositoryDto } from "@/types/repositories";

export function mapRepository(repo: RepositoryDto): Repository {
  return {
    id: repo.github_repository_id,
    githubRepositoryId: repo.github_repository_id,
    trackedRepositoryId: repo.tracked_repository_id ?? repo.id,
    installationId: repo.installation_id,
    fullName: repo.full_name,
    repositoryUrl: repo.repository_url,
    defaultBranch: repo.default_branch,
    trackedBranch: repo.tracked_branch,
    status: repo.status,
    indexedCommitSha: repo.indexed_commit_sha,
    name: repo.name,
    description: repo.description,
    isPrivate: repo.is_private,
    updatedAt: repo.updated_at,
  };
}
