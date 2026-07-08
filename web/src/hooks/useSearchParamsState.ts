import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * Thin wrapper over react-router's `useSearchParams` for reading/writing a
 * flat set of string params. `patch` deletes a key when given `undefined`
 * or `""` so cleared filters don't linger in the URL as `?cuisine=`, and
 * writes replace history (no back-button spam while filtering/typing).
 */
export function useSearchParamsState() {
  const [searchParams, setSearchParams] = useSearchParams();

  const get = useCallback(
    (key: string): string | undefined => searchParams.get(key) ?? undefined,
    [searchParams],
  );

  const patch = useCallback(
    (updates: Record<string, string | undefined>) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [key, value] of Object.entries(updates)) {
            if (value === undefined || value === "") {
              next.delete(key);
            } else {
              next.set(key, value);
            }
          }
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  return { searchParams, get, patch };
}
