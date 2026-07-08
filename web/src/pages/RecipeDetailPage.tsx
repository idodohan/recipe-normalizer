import { useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { EmptyState } from "../components/EmptyState";
import { toast } from "../hooks/useToast";
import { IngredientList } from "../components/recipe/IngredientList";
import type { DisplayGroup } from "../components/recipe/IngredientList";
import { ScaleControl } from "../components/recipe/ScaleControl";
import type { ScaleRequest } from "../components/recipe/ScaleControl";
import { StepList } from "../components/recipe/StepList";
import "../components/recipe/recipe-detail.css";

/** "3 min", "45 min", "1 hr 30 min" — cookbook shorthand. */
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

function DeleteControl({
  onConfirm,
  deleting,
}: {
  onConfirm: () => void;
  deleting: boolean;
}) {
  const [confirming, setConfirming] = useState(false);

  if (!confirming) {
    return (
      <button type="button" className="rd__delete" onClick={() => setConfirming(true)}>
        Delete
      </button>
    );
  }

  return (
    <span className="rd__confirm">
      <span className="rd__confirm-q">Delete recipe?</span>
      <button
        type="button"
        className="rd__confirm-btn rd__confirm-btn--yes"
        disabled={deleting}
        onClick={onConfirm}
      >
        {deleting ? "Deleting…" : "Yes"}
      </button>
      <span className="rd__confirm-sep" aria-hidden="true">
        /
      </span>
      <button
        type="button"
        className="rd__confirm-btn rd__confirm-btn--no"
        disabled={deleting}
        onClick={() => setConfirming(false)}
      >
        No
      </button>
    </span>
  );
}

export function RecipeDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [scale, setScale] = useState<ScaleRequest | null>(null);

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

  const scaled = useQuery({
    queryKey: ["recipe", id, "scaled", scale],
    enabled: Boolean(id && scale),
    placeholderData: keepPreviousData,
    retry: 1,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes/{recipe_id}/scaled", {
        params: {
          path: { recipe_id: id! },
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

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE("/api/recipes/{recipe_id}", {
        params: { path: { recipe_id: id! } },
      });
      if (error) throw error;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["recipes"] });
      toast({ title: "Recipe deleted", variant: "success" });
      navigate("/");
    },
  });

  if (recipe.isPending) {
    return <p className="rd-status">Opening the recipe…</p>;
  }

  if (recipe.isError || !recipe.data) {
    return (
      <EmptyState
        title="Recipe not found"
        body={
          <>
            We couldn’t open this recipe.{" "}
            <Link to="/">Back to your cookbook</Link>.
          </>
        }
      />
    );
  }

  const data = recipe.data;

  // The scaled view is live only when a scale is applied and the fetch
  // succeeded; on error we keep the unscaled recipe and show a banner.
  const scaledData = scale && !scaled.isError ? scaled.data : undefined;
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
          // Keep the cook's wording visible beneath the rescaled row —
          // passthrough lines already show it as their primary text.
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

  // "Serves 4", or "Makes 12 muffins" when the yield has its own unit.
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

  const scaledNote =
    scale && scaled.isFetching
      ? "Rescaling…"
      : scaledData
        ? scale?.kind === "servings"
          ? `Scaled for ${scale.servings} servings (×${formatFactor(scaledData.factor)}) — showing recalculated quantities`
          : `Scaled ×${formatFactor(scaledData.factor)} — showing recalculated quantities`
        : null;

  return (
    <article className="rd">
      <div className="rd__toolbar">
        <Link to="/" className="rd__back">
          ← Cookbook
        </Link>
        <DeleteControl
          onConfirm={() => remove.mutate()}
          deleting={remove.isPending}
        />
      </div>

      {remove.isError ? (
        <p className="rd__error" role="alert">
          {apiErrorMessage(remove.error, "Could not delete this recipe. Please try again.")}
        </p>
      ) : null}

      <header className="rd__header">
        <p className="rd__kicker">
          {data.dish_types.length > 0 ? data.dish_types.join(" · ") : "Recipe"}
        </p>
        <h1 className="rd__title">{data.title}</h1>
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
        {data.is_verified ? null : <span className="rd__flag">Unreviewed</span>}
        {quietTags.length > 0 ? (
          <span className="rd__taglist">{quietTags.join(" · ")}</span>
        ) : null}
      </p>

      <div className="rd__body">
        <aside className="rd__rail" aria-label="Ingredients">
          <h2 className="rd__colhead">Ingredients</h2>

          <ScaleControl
            scale={scale}
            onChange={setScale}
            baseServings={servings?.amount}
          />

          {scale && scaled.isError ? (
            <p className="rd__error" role="alert">
              {apiErrorMessage(
                scaled.error,
                "Could not scale this recipe — showing the original quantities.",
              )}
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
