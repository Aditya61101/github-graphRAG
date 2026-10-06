// hooks/useApi.ts
import { useState, useEffect, useCallback, useRef } from "react";
import axios, { AxiosError } from "axios";

interface UseApiState<T> {
  data: T;
  loading: boolean;
  error: string | null;
}

interface UseApiOptions<T, Args extends any[]> {
  immediate?: boolean;
  initialData: T;
  // We add initialArgs so the useEffect knows what to call the function with on mount
  initialArgs?: Args;
}

export function useApi<T, Args extends any[]>(
  apiFunction: (...args: Args) => Promise<T>,
  options: UseApiOptions<T, Args>
) {
  const {
    immediate = false,
    initialData,
    initialArgs = [] as unknown as Args,
  } = options;

  const [state, setState] = useState<UseApiState<T>>({
    data: initialData,
    loading: immediate,
    error: null,
  });

  const abortControllerRef = useRef<AbortController | null>(null);

  const execute = useCallback(
    async (...args: Args) => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      abortControllerRef.current = new AbortController();

      setState((prevState) => ({ ...prevState, loading: true, error: null }));

      try {
        const result = await apiFunction(...args);
        setState({ data: result, loading: false, error: null });
        return result;
      } catch (err) {
        if (axios.isCancel(err)) return;

        const axiosError = err as AxiosError<{ message?: string }>;
        const errorMessage =
          axiosError.response?.data?.message ||
          axiosError.message ||
          "An error occurred";

        setState((prevState) => ({
          ...prevState,
          loading: false,
          error: errorMessage,
        }));
        throw err;
      }
    },
    [apiFunction]
  );

  // RESTORED: The essential synchronization link for 'immediate' execution
  useEffect(() => {
    if (immediate) {
      execute(...initialArgs);
    }

    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [execute, immediate, JSON.stringify(initialArgs)]); // Stringify keeps primitive arrays safe from infinite loops

  return { ...state, execute };
}
