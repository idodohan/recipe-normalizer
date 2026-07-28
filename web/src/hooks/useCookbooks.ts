import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "../api/client";
import type { components } from "../api/schema";

export type CookbookSummary = components["schemas"]["CookbookSummary"];
export type CookbookDetail = components["schemas"]["CookbookDetailOut"];
export type CookbookOut = components["schemas"]["CookbookOut"];
export type Visibility = "private" | "unlisted" | "public";

/** All cookbooks the signed-in user can see: owned + shared-with-them. */
export function useCookbooks() {
  return useQuery({
    queryKey: ["cookbooks"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/cookbooks");
      if (error) throw error;
      return data;
    },
  });
}

/** One cookbook plus its recipes (access-checked server-side). */
export function useCookbook(id: string | undefined) {
  return useQuery({
    queryKey: ["cookbook", id],
    enabled: Boolean(id),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/cookbooks/{cookbook_id}", {
        params: { path: { cookbook_id: id! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useCreateCookbook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { name: string; description?: string | null }) => {
      const { data, error } = await api.POST("/api/cookbooks", { body });
      if (error) throw error;
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cookbooks"] }),
  });
}

export function usePatchCookbook(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      name?: string | null;
      description?: string | null;
      visibility?: Visibility | null;
    }) => {
      const { data, error } = await api.PATCH("/api/cookbooks/{cookbook_id}", {
        params: { path: { cookbook_id: id } },
        body,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["cookbook", id] });
      void qc.invalidateQueries({ queryKey: ["cookbooks"] });
    },
  });
}

export function useDeleteCookbook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.DELETE("/api/cookbooks/{cookbook_id}", {
        params: { path: { cookbook_id: id } },
      });
      if (error) throw error;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cookbooks"] }),
  });
}

export function useInviteMember(cookbookId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { email: string; role: "editor" | "viewer" }) => {
      const { data, error } = await api.POST(
        "/api/cookbooks/{cookbook_id}/members",
        { params: { path: { cookbook_id: cookbookId } }, body },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cookbook", cookbookId] }),
  });
}

export function useRemoveMember(cookbookId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (userId: string) => {
      const { error } = await api.DELETE(
        "/api/cookbooks/{cookbook_id}/members/{user_id}",
        { params: { path: { cookbook_id: cookbookId, user_id: userId } } },
      );
      if (error) throw error;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cookbook", cookbookId] }),
  });
}
