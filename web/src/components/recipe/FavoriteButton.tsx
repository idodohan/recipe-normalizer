import { useFavoriteMutation } from "../../hooks/useFavoriteMutation";
import "./favorite-button.css";

type FavoriteButtonProps = {
  recipeId: string;
  isFavorite: boolean;
  className?: string;
};

/**
 * Heart toggle shared by RecipeCard and RecipeDetailPage. A hairline
 * outline by default, filled terracotta once favorited — a 44px hit area
 * around a smaller glyph, per the touch-target minimum.
 */
export function FavoriteButton({ recipeId, isFavorite, className }: FavoriteButtonProps) {
  const mutation = useFavoriteMutation(recipeId);

  function handleClick(event: React.MouseEvent<HTMLButtonElement>) {
    // The card this button sits on is itself a Link — never let the click
    // bubble into a navigation.
    event.preventDefault();
    event.stopPropagation();
    mutation.mutate(!isFavorite);
  }

  const classes = ["favorite-btn"];
  if (isFavorite) classes.push("favorite-btn--active");
  if (className) classes.push(className);

  return (
    <button
      type="button"
      className={classes.join(" ")}
      aria-pressed={isFavorite}
      aria-label={isFavorite ? "Remove from favorites" : "Add to favorites"}
      onClick={handleClick}
      disabled={mutation.isPending}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true" className="favorite-btn__icon">
        <path
          d="M12 20.6s-7.29-4.6-10.06-8.86C.35 9.06 1.2 5.4 4.5 4.1c2.6-1.02 5.24-.1 7.5 2.86 2.26-2.96 4.9-3.88 7.5-2.86 3.3 1.3 4.15 4.96 2.56 7.64C19.29 16 12 20.6 12 20.6z"
          fill={isFavorite ? "currentColor" : "none"}
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}
