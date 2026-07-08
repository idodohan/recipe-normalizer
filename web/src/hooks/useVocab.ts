import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

/** Shared vocab list (units, ingredient names, etc.) — rarely changes. */
export function useVocab() {
  return useQuery({
    queryKey: ["vocab"],
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/vocab");
      if (error) throw error;
      return data;
    },
  });
}
