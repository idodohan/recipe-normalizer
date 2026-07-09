import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import "../skeleton.css";
import "./similar-recipes.css";

type RecipeSummary = components["schemas"]["RecipeSummary"];

/** "3 min", "45 min", "1 hr 30 min" — same shorthand as RecipeCard/RecipeDetailPage. */
function formatTotalTime(totalMin: number | null | undefined): string | null {
  if (totalMin == null || totalMin <= 0) return null;
  if (totalMin < 60) return `${totalMin} min`;
  const hours = Math.floor(totalMin / 60);
  const minutes = totalMin % 60;
  return minutes === 0 ? `${hours} hr` : `${hours} hr ${minutes} min`;
}

const SIMILAR_LIMIT = 6;

type SimilarRecipesProps = {
  recipeId: string;
};

/**
 * "More like this" — a quiet discovery row at the foot of the recipe, backed
 * by `GET /api/recipes/{recipe_id}/similar` (deterministic content-overlap
 * scoring, no LLM; see cookbook.service.recommendations_for_recipe). Purely
 * a nice-to-have: a loading skeleton stands in briefly, but the section
 * renders NOTHING — not a header, not an error banner — when there's no
 * overlap or the request fails, rather than leaving a heading dangling over
 * an empty/broken row.
 *
 * Query key is `["recipe", recipeId, "similar"]` — scoped to *recipeId*, so
 * navigating from one recipe to another (e.g. clicking a tile in this very
 * row) is a fresh query key and refetches for the new recipe rather than
 * carrying over the previous recipe's suggestions.
 */
export function SimilarRecipes({ recipeId }: SimilarRecipesProps) {
  const similar = useQuery({
    queryKey: ["recipe", recipeId, "similar"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes/{recipe_id}/similar", {
        params: { path: { recipe_id: recipeId }, query: { limit: SIMILAR_LIMIT } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (similar.isPending) {
    return (
      <section className="similar" aria-label="More like this, loading">
        <h2 className="similar__heading">More like this</h2>
        <ul className="similar__grid" aria-hidden="true">
          {Array.from({ length: 3 }, (_, i) => (
            <li key={i} className="similar__tile similar__tile--skel">
              <span className="skel similar__skel-kicker" style={{ width: "48%" }} />
              <span className="skel similar__skel-plate" />
              <span className="skel similar__skel-title" style={{ width: "82%" }} />
              <span className="skel similar__skel-title" style={{ width: "55%" }} />
            </li>
          ))}
        </ul>
      </section>
    );
  }

  // A failed fetch or a genuinely empty overlap both mean the same thing to
  // the reader — nothing to show — so the whole section (heading included)
  // is omitted rather than dangling over nothing or surfacing an error
  // banner for a feature that's a nice-to-have, not core recipe content.
  if (similar.isError || !similar.data || similar.data.length === 0) {
    return null;
  }

  return (
    <section className="similar" aria-labelledby="similar-heading">
      <h2 className="similar__heading" id="similar-heading">
        More like this
      </h2>
      <ul className="similar__grid">
        {similar.data.map((recipe) => (
          <li key={recipe.id}>
            <SimilarTile recipe={recipe} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function SimilarTile({ recipe }: { recipe: RecipeSummary }) {
  const time = formatTotalTime(recipe.total_min);
  const initial = [...recipe.title.trim()][0]?.toUpperCase() ?? "—";
  const meta = [recipe.dish_types.join(" · "), time].filter(Boolean).join(" — ");

  return (
    <Link to={`/recipes/${recipe.id}`} className="similar__tile">
      {recipe.image_ref ? (
        <img className="similar__image" src={recipe.image_ref} alt="" loading="lazy" />
      ) : (
        <span className="similar__initial" aria-hidden="true">
          {initial}
        </span>
      )}
      <span className="similar__title">{recipe.title}</span>
      {meta ? <span className="similar__meta">{meta}</span> : null}
    </Link>
  );
}
