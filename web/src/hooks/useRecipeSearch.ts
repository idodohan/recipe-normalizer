import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { api } from "../api/client";
import type { components } from "../api/schema";

export type CollectionOut = components["schemas"]["CollectionOut"];

/** The user's collections (cross-cookbook groupings of recipes). */
export function useCollections() {
  return useQuery({
    queryKey: ["collections"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/collections");
      if (error) throw error;
      return data;
    },
  });
}

export type RecipeSearchParams = {
  q?: string;
  collection?: string;
  favorites?: boolean;
};

/**
 * Search recipes across every cookbook the user can read (owned + shared),
 * via GET /api/recipes with the full-text `q` and the `collection`/`favorites`
 * filters. Only runs when there's an active query or filter — an empty search
 * leaves the cookbook grid in place.
 */
export function useRecipeSearch(params: RecipeSearchParams) {
  const active = Boolean(params.q?.trim() || params.collection || params.favorites);
  return useQuery({
    queryKey: ["recipes", "search", params],
    enabled: active,
    placeholderData: keepPreviousData,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes", {
        params: {
          query: {
            q: params.q?.trim() || undefined,
            collection: params.collection || undefined,
            favorites: params.favorites || undefined,
            limit: 60,
          },
        },
      });
      if (error) throw error;
      return data;
    },
  });
}
