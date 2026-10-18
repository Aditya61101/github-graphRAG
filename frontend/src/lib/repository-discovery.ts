import {
  CancelledError,
  isCancelledError,
  type QueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { repositoryService } from "@/api/repository-service";

export const repositoryDiscoveryOptions = (queryKey: QueryKey) => ({
  queryKey,
  queryFn: ({ signal }: { signal: AbortSignal }) =>
    repositoryService.getRepositories(signal),
  retry: false,
  staleTime: 0,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
});

export async function refreshRepositoryDiscovery(
  client: QueryClient,
  queryKey: QueryKey,
  afterMutation = false
) {
  // Verification must start after the POST, even if discovery was already
  // running when the server accepted the mutation. Cancellation discards late
  // results even when the adapter cannot abort its underlying transport.
  const originating = client.getQueryCache().find({ queryKey, exact: true });
  if (afterMutation) {
    await client.cancelQueries({ queryKey, exact: true });
    if (
      originating &&
      client.getQueryCache().find({ queryKey, exact: true }) !== originating
    ) {
      throw new CancelledError();
    }
  }
  try {
    return await client.fetchQuery(repositoryDiscoveryOptions(queryKey));
  } catch (error) {
    // A concurrent accepted mutation may supersede this collection read. Join
    // its replacement once, without starting a retry/polling loop.
    if (!isCancelledError(error) || !client.getQueryState(queryKey))
      throw error;
    return client.fetchQuery(repositoryDiscoveryOptions(queryKey));
  }
}
