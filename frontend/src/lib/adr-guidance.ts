import type { ADRDocument } from "@/types/knowledge";

// Best-effort session fallback when storage is blocked. These keys contain only
// account/repository identifiers, never tokens or document content.
const presented = new Set<string>();
type GuidanceStorage = Pick<Storage, "getItem" | "setItem">;

export function adrGuidanceKey(
  userId: string,
  repositoryId: string
): string | null {
  if (!userId.trim() || !/^repo_[A-Za-z0-9_-]+$/.test(repositoryId))
    return null;
  return `decisionguard:adr-guidance:v1:${encodeURIComponent(userId)}:${encodeURIComponent(repositoryId)}`;
}

export function hasSeenADRGuidance(
  key: string,
  storage?: GuidanceStorage
): boolean {
  if (presented.has(key)) return true;
  try {
    return (storage ?? window.localStorage).getItem(key) !== null;
  } catch {
    return false;
  }
}

// Called only after successful inventory validation and eligibility checks.
// Recording precedes presentation, so StrictMode and remounts cannot replay it.
export function claimADRGuidance(
  key: string,
  storage?: GuidanceStorage
): boolean {
  if (hasSeenADRGuidance(key, storage)) return false;
  presented.add(key);
  try {
    (storage ?? window.localStorage).setItem(key, "shown");
  } catch {
    // The Overview still works; the in-memory claim lasts for this app session.
  }
  return true;
}

export function isADRGuidanceEligible({
  status,
  canManage,
  inventory,
  succeeded,
  fetching,
  deletionEpoch,
  cleanup,
}: {
  status: string;
  canManage: boolean;
  inventory: ADRDocument[] | undefined;
  succeeded: boolean;
  fetching: boolean;
  deletionEpoch: number;
  cleanup: boolean;
}): boolean {
  return (
    status === "COMPLETED" &&
    canManage &&
    succeeded &&
    !fetching &&
    inventory !== undefined &&
    inventory.length === 0 &&
    deletionEpoch === 0 &&
    !cleanup
  );
}
