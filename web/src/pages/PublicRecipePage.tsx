import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { EmptyState } from "../components/EmptyState";
import { Skeleton } from "../components/Skeleton";
import { IngredientList } from "../components/recipe/IngredientList";
import type { DisplayGroup } from "../components/recipe/IngredientList";
import { ScaleControl } from "../components/recipe/ScaleControl";
import type { ScaleRequest } from "../components/recipe/ScaleControl";
import { StepList } from "../components/recipe/StepList";
import "../components/recipe/recipe-detail.css";
import "./public-recipe.css";

/** "3 min", "45 min", "1 hr 30 min" — cookbook shorthand (mirrors RecipeDetailPage). */
function formatMinutes(min: number | null | undefined): string | null {
  if (min == null || min <= 0) return null;
  if (min < 60) return `${min} min`;
  const hours = Math.floor(min / 60);
  const rest = min % 60;
  return rest === 0 ? `${hours} hr` : `${hours} hr ${rest} min`;
}

/** ×0.5 reads as ×½ in the scaled note; otherwise trim float noise. */
function formatFactor(factor: number): string {
  if (factor === 0.5) return "½";
  return String(Math.round(factor * 100) / 100);
}

/**
 * PublicRecipePage — the unauthenticated `/p/:token` view a public link
 * points at. Deliberately outside the AuthenticatedApp shell (see
 * main.tsx's router): no nav, no session, no favorite/notes/edit/collection
 * affordances, no auth redirect. Reuses the same dual-quantity renderers
 * (IngredientList/StepList/ScaleControl) and the recipe-detail.css `rd__*`
 * classes as the authenticated detail page, so a shared link looks like the
 * same cookbook plate — just with minimal chrome around it.
 */
export function PublicRecipePage() {
  const { token } = useParams<{ token: string }>();
  const [scale, setScale] = useState<ScaleRequest | null>(null);

  const recipe = useQuery({
    queryKey: ["public-recipe", token],
    enabled: Boolean(token),
    retry: false,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/public/{token}", {
        params: { path: { token: token! } },
      });
      if (error) throw error;
      return data;
    },
  });

  const scaled = useQuery({
    queryKey: ["public-recipe", token, "scaled", scale],
    enabled: Boolean(token && scale),
    placeholderData: keepPreviousData,
    retry: false,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/public/{token}/scaled", {
        params: {
          path: { token: token! },
          query:
            scale!.kind === "factor"
              ? { factor: scale!.factor }
              : { target_servings: scale!.servings },
        },
      });
      if (error) throw error;
      return data;
    },
  });

  return (
    <div className="prp">
      <header className="prp__topbar">
        <Link to="/" className="prp__wordmark">
          Recipe Normalizer
        </Link>
      </header>

      <main className="prp__main">
        {recipe.isPending ? <Skeleton variant="detail" /> : null}

        {recipe.isError || (!recipe.isPending && !recipe.data) ? (
          <EmptyState
            title="This link is no longer available"
            body="The recipe it pointed to may have been unshared, or the link was revoked."
            action={
              <Link to="/" className="btn btn--secondary btn--sm">
                Go to Recipe Normalizer
              </Link>
            }
          />
        ) : null}

        {recipe.data ? (
          <PublicRecipeBody
            data={recipe.data}
            scale={scale}
            onScale={setScale}
            scaled={scaled.data}
            scaledPending={Boolean(scale && scaled.isFetching)}
            scaledError={Boolean(scale && scaled.isError)}
          />
        ) : null}
      </main>

      <footer className="prp__footer">
        <Link to="/">Saved with Recipe Normalizer</Link>
      </footer>
    </div>
  );
}

type PublicRecipe = components["schemas"]["PublicRecipeOut"];
type ScaledRecipe = components["schemas"]["ScaledRecipeOut"];

function PublicRecipeBody({
  data,
  scale,
  onScale,
  scaled,
  scaledPending,
  scaledError,
}: {
  data: PublicRecipe;
  scale: ScaleRequest | null;
  onScale: (next: ScaleRequest | null) => void;
  scaled: ScaledRecipe | undefined;
  scaledPending: boolean;
  scaledError: boolean;
}) {
  const scaledData = scale && !scaledError ? scaled : undefined;
  const showScaled = Boolean(scaledData);

  const groups: DisplayGroup[] = scaledData
    ? scaledData.groups.map((group, gi) => ({
        key: `scaled-${gi}`,
        name: group.name,
        lines: group.lines.map((line, li) => ({
          key: `scaled-${gi}-${li}`,
          display: line.display,
          note: line.note,
          isOptional: line.is_optional,
          passesThrough: line.passes_through,
          originalText:
            !line.passes_through && line.quantity_display != null
              ? line.original_text
              : null,
        })),
      }))
    : data.groups.map((group) => ({
        key: group.id,
        name: group.name,
        lines: group.lines.map((line) => ({
          key: line.id,
          display: line.display,
          note: line.note,
          isOptional: line.is_optional,
        })),
      }));

  const steps = scaledData ? scaledData.steps : data.steps;

  const servings = data.servings;
  const servingsUnit = servings?.unit_text?.trim() ?? "";
  const unitIsServings = /^servings?$/i.test(servingsUnit);
  const servingsValue = servings?.amount
    ? unitIsServings || !servingsUnit
      ? String(servings.amount)
      : `${servings.amount} ${servingsUnit}`
    : servingsUnit || null;
  const servingsTerm = unitIsServings || !servingsUnit ? "Serves" : "Makes";
  const prep = formatMinutes(data.prep_min);
  const cook = formatMinutes(data.cook_min);
  const total = formatMinutes(data.total_min);
  const quietTags = [...data.cuisines, ...data.tags];

  const scaledNote = scaledPending
    ? "Rescaling…"
    : scaledData
      ? scale?.kind === "servings"
        ? `Scaled for ${scale.servings} servings (×${formatFactor(scaledData.factor)}) — showing recalculated quantities`
        : `Scaled ×${formatFactor(scaledData.factor)} — showing recalculated quantities`
      : null;

  return (
    <article className="rd">
      {data.image_url ? (
        <div className="rd__image">
          <img className="rd__image-img" src={data.image_url} alt="" loading="lazy" />
          <div className="rd__image-scrim" aria-hidden="true" />
        </div>
      ) : null}

      <header className="rd__header">
        <div className="rd__heading-row">
          <div>
            <p className="rd__kicker">
              {data.dish_types.length > 0 ? data.dish_types.join(" · ") : "Recipe"}
            </p>
            <h1 className="rd__title">{data.title}</h1>
          </div>
        </div>
        {data.description ? <p className="rd__desc">{data.description}</p> : null}
      </header>

      <p className="rd__meta">
        {servingsValue ? (
          <span className="rd__meta-item">
            <span className="rd__meta-label">{servingsTerm}</span>
            {servingsValue}
          </span>
        ) : null}
        {prep ? (
          <span className="rd__meta-item">
            <span className="rd__meta-label">Prep</span>
            {prep}
          </span>
        ) : null}
        {cook ? (
          <span className="rd__meta-item">
            <span className="rd__meta-label">Cook</span>
            {cook}
          </span>
        ) : null}
        {total ? (
          <span className="rd__meta-item">
            <span className="rd__meta-label">Total</span>
            {total}
          </span>
        ) : null}
        {quietTags.length > 0 ? (
          <span className="rd__taglist">{quietTags.join(" · ")}</span>
        ) : null}
      </p>

      <div className="rd__body">
        <aside className="rd__rail" aria-label="Ingredients">
          <h2 className="rd__colhead">Ingredients</h2>

          <ScaleControl scale={scale} onChange={onScale} baseServings={servings?.amount} />

          {scaledError ? (
            <p className="rd__error" role="alert">
              Could not scale this recipe — showing the original quantities.
            </p>
          ) : null}

          {scaledNote ? <p className="rd__scaled-note">{scaledNote}</p> : null}

          <IngredientList groups={groups} />
        </aside>

        <section className="rd__method">
          <h2 className="rd__colhead">Method</h2>

          {showScaled && scaledData?.step_text_disclaimer ? (
            <p className="rd__note">
              <strong>Note</strong>
              Step text shows original quantities
            </p>
          ) : null}

          <StepList steps={steps} />
        </section>
      </div>
    </article>
  );
}
