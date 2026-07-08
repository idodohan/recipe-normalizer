import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import type { components } from "../api/schema";
import { toast } from "./useToast";

type RecipePage = components["schemas"]["RecipePage"];
type RecipeOut = components["schemas"]["RecipeOut"];
type RecipeListData = InfiniteData<RecipePage>;

type FavoriteContext = {
  previousLists: Array<[readonly unknown[], RecipeListData | undefined]>;
  previousDetail?: RecipeOut;
};

function withFavoritePatched(recipeId: string, isFavorite: boolean) {
  return (page: RecipePage): RecipePage => ({
    ...page,
    items: page.items.map((recipe) =>
      recipe.id === recipeId ? { ...recipe, is_favorite: isFavorite } : recipe,
    ),
  });
}

/**
 * Optimistic favorite toggle shared by the cookbook grid (RecipeCard) and
 * the detail page header. The cookbook list is cached per filter/search
 * combination (`["recipes", filters]`, paginated via `useInfiniteQuery`), so
 * this patches every cached `["recipes", ...]` query — not just the one
 * currently mounted — plus the `["recipe", id]` detail cache, then rolls
 * both back with an error toast if the PATCH fails.
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

      const previousLists = queryClient.getQueriesData<RecipeListData>({
        queryKey: ["recipes"],
      });
      const previousDetail = queryClient.getQueryData<RecipeOut>(["recipe", recipeId]);

      const patchPage = withFavoritePatched(recipeId, isFavorite);
      queryClient.setQueriesData<RecipeListData>({ queryKey: ["recipes"] }, (current) => {
        // Guard against unexpected cache shape; InfiniteData always has pages array
        if (!current || !Array.isArray((current as {pages?: unknown}).pages)) return current;
        return { ...current, pages: current.pages.map(patchPage) };
      });

      if (previousDetail) {
        queryClient.setQueryData<RecipeOut>(["recipe", recipeId], {
          ...previousDetail,
          is_favorite: isFavorite,
        });
      }

      return { previousLists, previousDetail };
    },
    onError: (error, _isFavorite, context) => {
      context?.previousLists.forEach(([key, data]) => {
        queryClient.setQueryData(key, data);
      });
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
