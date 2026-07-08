import { useEffect, useState } from "react";
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { PageHeader } from "../components/PageHeader";
import { RecipeCard } from "../components/recipe/RecipeCard";
import { Skeleton } from "../components/Skeleton";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useSearchParamsState } from "../hooks/useSearchParamsState";
import { useVocab } from "../hooks/useVocab";
import "./cookbook.css";

type Dietary = "vegan" | "vegetarian" | "gluten_free";

const DIETARY_OPTIONS: { value: Dietary; label: string }[] = [
  { value: "vegan", label: "Vegan" },
  { value: "vegetarian", label: "Vegetarian" },
  { value: "gluten_free", label: "Gluten-free" },
];

function parseDietary(raw: string | null | undefined): Dietary | undefined {
  if (raw === "vegan" || raw === "vegetarian" || raw === "gluten_free") {
    return raw;
  }
  return undefined;
}

const PAGE_SIZE = 24;

// Pagination approach: `useInfiniteQuery` (offset-paged via `pageParam`),
// not manual page-state accumulation — React Query already tracks pages
// per query key, dedupes in-flight requests, and exposes hasNextPage /
// fetchNextPage for free, so it's the cleaner of the two options for a
// straightforward "Load more" button. See useFavoriteMutation.ts for how
// the resulting InfiniteData<RecipePage> cache shape is patched for the
// optimistic heart toggle.
export function CookbookPage() {
  const navigate = useNavigate();
  const { get, patch } = useSearchParamsState();
  const vocab = useVocab();

  const collections = useQuery({
    queryKey: ["collections"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/collections");
      if (error) throw error;
      return data;
    },
  });

  const urlQuery = get("q") ?? "";
  const [queryInput, setQueryInput] = useState(urlQuery);
  const debouncedQuery = useDebouncedValue(queryInput, 300);

  const cuisine = get("cuisine") ?? "";
  const dishType = get("dish_type") ?? "";
  const tag = get("tag") ?? "";
  const dietary = parseDietary(get("dietary"));
  const favorites = get("favorites") === "true";
  const collection = get("collection") ?? "";

  // Debounce still settling — don't commit the in-flight keystroke to the
  // URL (or the query key) until it settles, same pattern as the catalog.
  const isSettling = queryInput !== debouncedQuery;

  const filters = {
    q: debouncedQuery || undefined,
    cuisine: cuisine || undefined,
    dish_type: dishType || undefined,
    tag: tag || undefined,
    dietary,
    favorites: favorites || undefined,
    collection: collection || undefined,
  };

  const hasActiveFilters = Object.values(filters).some((value) => value !== undefined);

  const recipes = useInfiniteQuery({
    queryKey: ["recipes", filters],
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET("/api/recipes", {
        params: {
          query: {
            ...filters,
            limit: PAGE_SIZE,
            offset: pageParam,
          },
        },
      });
      if (error) throw error;
      return data;
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const loaded = lastPage.offset + lastPage.items.length;
      return loaded < lastPage.total ? loaded : undefined;
    },
    placeholderData: keepPreviousData,
  });

  function setQuery(value: string) {
    setQueryInput(value);
  }

  function setFilter(key: string, value: string | undefined) {
    patch({ [key]: value });
  }

  function toggleDietary(value: Dietary) {
    patch({ dietary: dietary === value ? undefined : value });
  }

  function setFavorites(next: boolean) {
    patch({ favorites: next ? "true" : undefined });
  }

  function clearAll() {
    setQueryInput("");
    patch({
      q: undefined,
      cuisine: undefined,
      dish_type: undefined,
      tag: undefined,
      dietary: undefined,
      favorites: undefined,
      collection: undefined,
    });
  }

  // Commit the debounced search text to the URL once typing settles, so the
  // search is shareable/back-button-friendly without rewriting the URL on
  // every keystroke.
  useEffect(() => {
    if (debouncedQuery !== urlQuery) {
      patch({ q: debouncedQuery || undefined });
    }
  }, [debouncedQuery, urlQuery, patch]);

  // Back/forward navigation changes the URL without the user typing —
  // resync the input in that case. Intentionally omits `debouncedQuery`
  // from deps: it changes on every settled keystroke (handled by the
  // effect above), and reacting to it here too would immediately stomp the
  // text the user just typed before that effect's URL commit lands.
  /* eslint-disable-next-line react-hooks/exhaustive-deps */
  useEffect(() => {
    if (urlQuery !== debouncedQuery) {
      // Reseeding local input state from the URL (an external system) on
      // back/forward nav, not a derived-state sync — the cascading-render
      // warning doesn't apply here.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setQueryInput(urlQuery);
    }
  }, [urlQuery]);

  const items = recipes.data?.pages.flatMap((page) => page.items) ?? [];
  const total = recipes.data?.pages[0]?.total ?? 0;
  const showSkeleton = recipes.isPending || (isSettling && !recipes.isError);

  return (
    <>
      <PageHeader
        overline="Your collection"
        title="Cookbook"
        subtitle="Recipes you have saved and normalized."
        action={
          <Button variant="secondary" onClick={() => navigate("/recipes/new")}>
            Add recipe
          </Button>
        }
      />

      <div className="cookbook-search">
        <input
          type="search"
          className="input cookbook-search__input"
          placeholder="Search by title or description…"
          aria-label="Search recipes"
          value={queryInput}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      <div className="cookbook-filters" role="group" aria-label="Filter recipes">
        <button
          type="button"
          className="chip chip--filter"
          aria-pressed={!favorites}
          onClick={() => setFavorites(false)}
        >
          All
        </button>
        <button
          type="button"
          className="chip chip--filter"
          aria-pressed={favorites}
          onClick={() => setFavorites(true)}
        >
          Favorites
        </button>

        {DIETARY_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            className="chip chip--filter"
            aria-pressed={dietary === option.value}
            onClick={() => toggleDietary(option.value)}
          >
            {option.label}
          </button>
        ))}

        <select
          className="input cookbook-filters__select"
          aria-label="Cuisine"
          value={cuisine}
          onChange={(event) => setFilter("cuisine", event.target.value)}
        >
          <option value="">Any cuisine</option>
          {(vocab.data?.cuisines ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        <select
          className="input cookbook-filters__select"
          aria-label="Dish type"
          value={dishType}
          onChange={(event) => setFilter("dish_type", event.target.value)}
        >
          <option value="">Any dish type</option>
          {(vocab.data?.dish_types ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        <select
          className="input cookbook-filters__select"
          aria-label="Tag"
          value={tag}
          onChange={(event) => setFilter("tag", event.target.value)}
        >
          <option value="">Any tag</option>
          {(vocab.data?.tags ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        <select
          className="input cookbook-filters__select"
          aria-label="Collection"
          value={collection}
          onChange={(event) => setFilter("collection", event.target.value)}
        >
          <option value="">Any collection</option>
          {(collections.data ?? []).map((option) => (
            <option key={option.id} value={option.id}>
              {option.name} ({option.recipe_count})
            </option>
          ))}
        </select>

        {hasActiveFilters ? (
          <button type="button" className="cookbook-clear" onClick={clearAll}>
            Clear filters
          </button>
        ) : null}
      </div>

      {!showSkeleton && !recipes.isError ? (
        <p className="cookbook-count">
          {hasActiveFilters
            ? `${total} ${total === 1 ? "match" : "matches"}`
            : `${total} ${total === 1 ? "recipe" : "recipes"} — newest first.`}
        </p>
      ) : null}

      {showSkeleton ? (
        <Skeleton variant="card-grid" />
      ) : recipes.isError ? (
        <ErrorState
          message={apiErrorMessage(
            recipes.error,
            "Could not load your cookbook. Please try again.",
          )}
          onRetry={() => void recipes.refetch()}
        />
      ) : total === 0 ? (
        hasActiveFilters ? (
          <EmptyState
            title="Nothing matches — clear the filters?"
            action={
              <Button variant="secondary" onClick={clearAll}>
                Clear filters
              </Button>
            }
          />
        ) : (
          <EmptyState
            title="Your cookbook is empty"
            body="When you add recipes, they will appear here — ingredients normalized, quantities ready to scale."
            action={
              <Button onClick={() => navigate("/recipes/new")}>
                Add a recipe
              </Button>
            }
          />
        )
      ) : (
        <>
          <ul className="cookbook-grid">
            {items.map((recipe, index) => (
              <li key={recipe.id}>
                <RecipeCard recipe={recipe} index={index} />
              </li>
            ))}
          </ul>

          {recipes.hasNextPage ? (
            <div className="cookbook-load-more">
              <Button
                variant="secondary"
                onClick={() => void recipes.fetchNextPage()}
                disabled={recipes.isFetchingNextPage}
              >
                {recipes.isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            </div>
          ) : null}
        </>
      )}
    </>
  );
}
