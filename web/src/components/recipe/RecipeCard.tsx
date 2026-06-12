import { Link } from "react-router-dom";
import type { components } from "../../api/schema";
import "./recipe-card.css";

type RecipeSummary = components["schemas"]["RecipeSummary"];

/** "3 min", "45 min", "3 hr", "1 hr 30 min" — cookbook shorthand. */
function formatTotalTime(totalMin: number | null | undefined): string | null {
  if (totalMin == null || totalMin <= 0) return null;
  if (totalMin < 60) return `${totalMin} min`;
  const hours = Math.floor(totalMin / 60);
  const minutes = totalMin % 60;
  return minutes === 0 ? `${hours} hr` : `${hours} hr ${minutes} min`;
}

type RecipeCardProps = {
  recipe: RecipeSummary;
  /** Position in the grid — drives the plate number and the ink/terracotta
   *  alternation that keeps the grid from feeling stamped. */
  index: number;
};

export function RecipeCard({ recipe, index }: RecipeCardProps) {
  const time = formatTotalTime(recipe.total_min);
  const initial = [...recipe.title.trim()][0]?.toUpperCase() ?? "—";
  const variant = index % 2 === 1 ? "recipe-card recipe-card--ink" : "recipe-card";

  return (
    <Link to={`/recipes/${recipe.id}`} className={variant}>
      <p className="recipe-card__kicker">
        <span className="recipe-card__types">
          {recipe.dish_types.length > 0 ? recipe.dish_types.join(" · ") : "recipe"}
        </span>
        <span className="recipe-card__no" aria-hidden="true">
          No. {index + 1}
        </span>
      </p>

      {recipe.image_ref ? (
        <img
          className="recipe-card__image"
          src={recipe.image_ref}
          alt=""
          loading="lazy"
        />
      ) : (
        <span className="recipe-card__initial" aria-hidden="true">
          {initial}
        </span>
      )}

      <h2 className="recipe-card__title">{recipe.title}</h2>

      <p className="recipe-card__foot">
        <span className="recipe-card__time">{time ?? "—"}</span>
        {recipe.is_verified ? null : (
          <span className="recipe-card__flag">Unreviewed</span>
        )}
      </p>
    </Link>
  );
}
