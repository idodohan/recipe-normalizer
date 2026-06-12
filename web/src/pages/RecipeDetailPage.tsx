import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

/**
 * Minimal recipe detail placeholder so navigation after save lands somewhere
 * real. Task 22 replaces this with the full recipe view.
 */
export function RecipeDetailPage() {
  const { id } = useParams<{ id: string }>();

  const recipe = useQuery({
    queryKey: ["recipe", id],
    enabled: Boolean(id),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes/{recipe_id}", {
        params: { path: { recipe_id: id! } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (recipe.isLoading) {
    return null;
  }

  if (recipe.isError || !recipe.data) {
    return (
      <EmptyState
        title="Recipe not found"
        body={
          <>
            We couldn’t open this recipe. <Link to="/">Back to your cookbook</Link>.
          </>
        }
      />
    );
  }

  return (
    <>
      <PageHeader
        overline="Recipe"
        title={recipe.data.title}
        subtitle={recipe.data.description ?? undefined}
      />
      <p style={{ color: "var(--color-text-muted)" }}>
        Saved to your cookbook. The full recipe view — normalized measures,
        scaling, and sharing — is on its way.{" "}
        <Link to="/">Back to your cookbook</Link>.
      </p>
    </>
  );
}
