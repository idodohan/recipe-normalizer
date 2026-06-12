import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { components } from "../api/schema";

export type User = components["schemas"]["UserOut"];

export const USER_QUERY_KEY = ["auth", "me"] as const;

async function fetchUser(): Promise<User | null> {
  const { data, response } = await api.GET("/api/auth/me");
  if (data) return data;
  if (response.status === 401 || response.status === 403) return null;
  throw new Error(`Unexpected response from /api/auth/me (${response.status})`);
}

/** Current session user; `null` when unauthenticated. */
export function useUser() {
  const query = useQuery({
    queryKey: USER_QUERY_KEY,
    queryFn: fetchUser,
    staleTime: 60_000,
    retry: false,
  });
  return {
    user: query.data ?? null,
    isLoading: query.isLoading,
    isError: query.isError,
  };
}

/** Imperative helpers for login/logout flows. */
export function useUserActions() {
  const queryClient = useQueryClient();
  return {
    setUser(user: User | null) {
      queryClient.setQueryData(USER_QUERY_KEY, user);
    },
    async clear() {
      queryClient.setQueryData(USER_QUERY_KEY, null);
      await queryClient.invalidateQueries({ queryKey: USER_QUERY_KEY });
    },
  };
}
