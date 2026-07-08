import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import type { components } from "../api/schema";
import { toast } from "./useToast";

type RecipeSummary = components["schemas"]["RecipeSummary"];
type RecipeOut = components["schemas"]["RecipeOut"];

type FavoriteContext = {
  previousList?: RecipeSummary[];
  previousDetail?: RecipeOut;
};

/**
 * Optimistic favorite toggle shared by the cookbook grid (RecipeCard) and
 * the detail page header. Patches the `["recipes"]` list cache and the
 * `["recipe", id]` detail cache immediately, then rolls both back with an
 * error toast if the PATCH fails.
 */
export function useFavoriteMutation(recipeId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (isFavorite: boolean) => {
      const { data, error } = await api.PATCH("/api/recipes/{recipe_id}/personal", {
        params: { path: { recipe_id: recipeId } },
        body: { is_favorite: isFavorite },
      });
      if (error) throw error;
      return data;
    },
    onMutate: async (isFavorite: boolean): Promise<FavoriteContext> => {
      await Promise.all([
        queryClient.cancelQueries({ queryKey: ["recipes"] }),
        queryClient.cancelQueries({ queryKey: ["recipe", recipeId] }),
      ]);

      const previousList = queryClient.getQueryData<RecipeSummary[]>(["recipes"]);
      const previousDetail = queryClient.getQueryData<RecipeOut>(["recipe", recipeId]);

      if (previousList) {
        queryClient.setQueryData<RecipeSummary[]>(
          ["recipes"],
          previousList.map((recipe) =>
            recipe.id === recipeId ? { ...recipe, is_favorite: isFavorite } : recipe,
          ),
        );
      }

      if (previousDetail) {
        queryClient.setQueryData<RecipeOut>(["recipe", recipeId], {
          ...previousDetail,
          is_favorite: isFavorite,
        });
      }

      return { previousList, previousDetail };
    },
    onError: (error, _isFavorite, context) => {
      if (context?.previousList) {
        queryClient.setQueryData(["recipes"], context.previousList);
      }
      if (context?.previousDetail) {
        queryClient.setQueryData(["recipe", recipeId], context.previousDetail);
      }
      toast({
        title: "Could not update favorite",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["recipes"] });
      void queryClient.invalidateQueries({ queryKey: ["recipe", recipeId] });
    },
  });
}
