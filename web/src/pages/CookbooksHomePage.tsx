import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Skeleton } from "../components/Skeleton";
import { CookbookCard } from "../components/cookbook/CookbookCard";
import { NewCookbookDialog } from "../components/cookbook/NewCookbookDialog";
import { CookbookQaPanel } from "../components/cookbook/CookbookQaPanel";
import { RecipeCard } from "../components/recipe/RecipeCard";
import { useCookbooks } from "../hooks/useCookbooks";
import { useRecipeSearch } from "../hooks/useRecipeSearch";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { apiErrorMessage } from "../api/errors";
import "../components/cookbook/cookbooks-home.css";

export function CookbooksHomePage() {
  const navigate = useNavigate();
  const cookbooks = useCookbooks();
  const [newOpen, setNewOpen] = useState(false);

  // Search across all readable cookbooks. A single calm field + a favorites
  // toggle — deliberately NOT the old nine-dropdown bar.
  const [queryInput, setQueryInput] = useState("");
  const query = useDebouncedValue(queryInput, 250);
  const [favorites, setFavorites] = useState(false);
  const searching = Boolean(query.trim() || favorites);
  const results = useRecipeSearch({
    q: query,
    favorites,
  });

  const items = cookbooks.data ?? [];
  const mine = items.filter((c) => c.role === "owner");
  const shared = items.filter((c) => c.role !== "owner");
  const totalRecipes = items.reduce((n, c) => n + c.recipe_count, 0);
  const resultItems = results.data?.items ?? [];

  function clearSearch() {
    setQueryInput("");
    setFavorites(false);
  }

  return (
    <>
      <header className="cb-home__masthead">
        <div>
          <p className="cb-home__overline">Your kitchen</p>
          <h1 className="cb-home__title">Cookbooks</h1>
        </div>
        <div className="cb-home__actions">
          <Button variant="secondary" onClick={() => setNewOpen(true)}>
            New cookbook
          </Button>
          <Button onClick={() => navigate("/add")}>Add a recipe</Button>
        </div>
      </header>

      {/* Ask across everything you've saved — AI, grounded in your recipes. */}
      <CookbookQaPanel />

      {/* Plain search across all cookbooks + a favorites scope. */}
      <div className="cb-search">
        <input
          type="search"
          className="cb-search__input"
          placeholder="Search all your recipes…"
          aria-label="Search your recipes"
          value={queryInput}
          onChange={(e) => setQueryInput(e.target.value)}
          maxLength={200}
        />
        <div className="cb-search__scopes" role="group" aria-label="Filters">
          <button
            type="button"
            className="chip chip--filter"
            aria-pressed={favorites}
            onClick={() => setFavorites((f) => !f)}
          >
            <span aria-hidden="true">♥ </span>Favorites
          </button>
          {searching ? (
            <button type="button" className="cb-search__clear" onClick={clearSearch}>
              Clear
            </button>
          ) : null}
        </div>
      </div>

      {searching ? (
        results.isPending ? (
          <Skeleton variant="card-grid" />
        ) : results.isError ? (
          <ErrorState
            message={apiErrorMessage(results.error, "Search failed.")}
            onRetry={() => void results.refetch()}
          />
        ) : resultItems.length === 0 ? (
          <EmptyState
            title="Nothing matches"
            body="Try a different word, or clear the filters."
            action={
              <Button variant="secondary" onClick={clearSearch}>
                Clear search
              </Button>
            }
          />
        ) : (
          <>
            <p className="cb-home__result-count">
              {results.data?.total ?? resultItems.length}{" "}
              {(results.data?.total ?? resultItems.length) === 1
                ? "recipe"
                : "recipes"}{" "}
              found
            </p>
            <ul className="cb-grid cb-grid--recipes">
              {resultItems.map((recipe, index) => (
                <li key={recipe.id}>
                  <RecipeCard recipe={recipe} index={index} />
                </li>
              ))}
            </ul>
          </>
        )
      ) : cookbooks.isPending ? (
        <Skeleton variant="card-grid" />
      ) : cookbooks.isError ? (
        <ErrorState
          message={apiErrorMessage(cookbooks.error, "Could not load your cookbooks.")}
          onRetry={() => void cookbooks.refetch()}
        />
      ) : totalRecipes === 0 && items.length <= 1 ? (
        <EmptyState
          title="Your cookbook is empty"
          body="Welcome! This app helps you organize and scale your recipes. Paste a link, drop a PDF or photo, or type a recipe in — we normalize every amount to grams and millilitres so you can cook with confidence."
          action={
            <div className="cb-home__firstrun">
              <Button onClick={() => navigate("/add")}>Add your first recipe</Button>
            </div>
          }
        />
      ) : (
        <>
          <section className="cb-home__section">
            {shared.length > 0 ? (
              <h2 className="cb-home__section-title">Yours</h2>
            ) : null}
            <ul className="cb-grid">
              {mine.map((c) => (
                <li key={c.id}>
                  <CookbookCard cookbook={c} />
                </li>
              ))}
            </ul>
          </section>

          {shared.length > 0 ? (
            <section className="cb-home__section">
              <h2 className="cb-home__section-title">Shared with you</h2>
              <ul className="cb-grid">
                {shared.map((c) => (
                  <li key={c.id}>
                    <CookbookCard cookbook={c} />
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </>
      )}

      <NewCookbookDialog open={newOpen} onClose={() => setNewOpen(false)} />
    </>
  );
}
