import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { PageHeader } from "../components/PageHeader";
import { RecipeCard } from "../components/recipe/RecipeCard";
import { Skeleton } from "../components/Skeleton";
import "./cookbook.css";

type Filter = "all" | "favorites";

export function CookbookPage() {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<Filter>("all");

  const recipes = useQuery({
    queryKey: ["recipes"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes");
      if (error) throw error;
      return data;
    },
  });

  const count = recipes.data?.length ?? 0;
  const visible =
    filter === "favorites"
      ? (recipes.data ?? []).filter((recipe) => recipe.is_favorite)
      : (recipes.data ?? []);

  return (
    <>
      <PageHeader
        overline="Your collection"
        title="Cookbook"
        subtitle={
          recipes.isSuccess && count > 0
            ? `${count} ${count === 1 ? "recipe" : "recipes"} — newest first.`
            : "Recipes you have saved and normalized."
        }
        action={
          <Button variant="secondary" onClick={() => navigate("/recipes/new")}>
            Add recipe
          </Button>
        }
      />

      {recipes.isPending ? (
        <Skeleton variant="card-grid" />
      ) : recipes.isError ? (
        <ErrorState
          message={apiErrorMessage(
            recipes.error,
            "Could not load your cookbook. Please try again.",
          )}
          onRetry={() => void recipes.refetch()}
        />
      ) : count === 0 ? (
        <EmptyState
          title="Your cookbook is empty"
          body="When you add recipes, they will appear here — ingredients normalized, quantities ready to scale."
          action={
            <Button onClick={() => navigate("/recipes/new")}>
              Add a recipe
            </Button>
          }
        />
      ) : (
        <>
          <div className="cookbook-filters" role="group" aria-label="Filter recipes">
            <button
              type="button"
              className="chip chip--filter"
              aria-pressed={filter === "all"}
              onClick={() => setFilter("all")}
            >
              All
            </button>
            <button
              type="button"
              className="chip chip--filter"
              aria-pressed={filter === "favorites"}
              onClick={() => setFilter("favorites")}
            >
              Favorites
            </button>
          </div>

          {visible.length === 0 ? (
            <EmptyState title="No favorites yet — tap the heart on any recipe." />
          ) : (
            <ul className="cookbook-grid">
              {visible.map((recipe, index) => (
                <li key={recipe.id}>
                  <RecipeCard recipe={recipe} index={index} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </>
  );
}
