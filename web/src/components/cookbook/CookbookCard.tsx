import { Link } from "react-router-dom";
import { GeneratedCover } from "../GeneratedCover";
import type { CookbookSummary } from "../../hooks/useCookbooks";
import "./cookbook-card.css";

const VISIBILITY_LABEL: Record<string, string> = {
  private: "Private",
  unlisted: "Shared by link",
  public: "Public",
};

/**
 * A photo-forward cookbook tile: cover (real or generated), name, recipe
 * count, and small badges for visibility + your role when it isn't yours.
 * Soft shadow, rounded — the "warm magazine" language, no hairline box.
 */
export function CookbookCard({ cookbook }: { cookbook: CookbookSummary }) {
  const count = cookbook.recipe_count;
  const shared = cookbook.role !== "owner";
  return (
    <Link to={`/cookbooks/${cookbook.id}`} className="cb-card">
      <div className="cb-card__cover">
        {cookbook.cover_image_ref ? (
          <img
            className="cb-card__img"
            src={cookbook.cover_image_ref}
            alt=""
            loading="lazy"
          />
        ) : (
          <GeneratedCover seed={cookbook.id} label={cookbook.name} />
        )}
        <div className="cb-card__badges">
          {cookbook.visibility !== "private" ? (
            <span className="cb-badge cb-badge--vis">
              {VISIBILITY_LABEL[cookbook.visibility] ?? cookbook.visibility}
            </span>
          ) : null}
          {shared ? (
            <span className="cb-badge cb-badge--role">
              Shared · {cookbook.role}
            </span>
          ) : null}
        </div>
      </div>
      <div className="cb-card__body">
        <h2 className="cb-card__name">{cookbook.name}</h2>
        <p className="cb-card__count">
          {count} {count === 1 ? "recipe" : "recipes"}
        </p>
      </div>
    </Link>
  );
}
