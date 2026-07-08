import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryKey } from "@tanstack/react-query";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { toast } from "../hooks/useToast";
import { useUser } from "../hooks/useUser";
import type { components } from "../api/schema";
import "./catalog.css";

type IngredientOut = components["schemas"]["IngredientOut"];
type IngredientStatus = components["schemas"]["IngredientStatus"];
type IngredientPatch = components["schemas"]["IngredientPatch"];

const STATUS_OPTIONS: IngredientStatus[] = ["seeded", "unreviewed", "reviewed"];
const STATUS_LABEL: Record<IngredientStatus, string> = {
  seeded: "Seeded",
  unreviewed: "Unreviewed",
  reviewed: "Reviewed",
};

/** Debounces a fast-changing value — used to throttle the catalog search query. */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

export function CatalogPage() {
  const { user } = useUser();
  const [query, setQuery] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const debouncedQuery = useDebouncedValue(query, 300);

  const queryKey = ["catalog", debouncedQuery] as const;

  const ingredients = useQuery({
    queryKey,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/catalog/ingredients", {
        params: { query: { q: debouncedQuery, limit: 200 } },
      });
      if (error) throw error;
      return data;
    },
  });

  // Debounce still settling — the fetch for the latest keystroke hasn't
  // fired yet, so keep the rows skeleton up rather than flash stale results.
  const isSettling = query !== debouncedQuery;
  const showSkeleton = ingredients.isPending || (isSettling && !ingredients.isError);

  return (
    <>
      <PageHeader
        overline="Reference"
        title="Catalog"
        subtitle="Canonical ingredients and units."
      />

      <p className="catalog-explainer">
        The shared ingredient library that powers unit conversion.
      </p>

      <div className="catalog-search">
        <input
          type="search"
          className="input catalog-search__input"
          placeholder="Search by name or alias…"
          aria-label="Search ingredients"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      {showSkeleton ? (
        <Skeleton variant="rows" count={8} />
      ) : ingredients.isError ? (
        <ErrorState
          message={apiErrorMessage(
            ingredients.error,
            "Could not load the catalog. Please try again.",
          )}
          onRetry={() => void ingredients.refetch()}
        />
      ) : ingredients.data.length === 0 ? (
        <EmptyState title="Nothing matches — try another name or alias." />
      ) : (
        <ul className="catalog-table">
          <li className="catalog-row catalog-row--head" aria-hidden="true">
            <span className="catalog-col catalog-col--name">Name</span>
            <span className="catalog-col catalog-col--category">Category</span>
            <span className="catalog-col catalog-col--density">Density (g/ml)</span>
            <span className="catalog-col catalog-col--status">Status</span>
            <span className="catalog-col catalog-col--flags">Dietary flags</span>
          </li>
          {ingredients.data.map((ingredient) => (
            <IngredientRow
              key={ingredient.id}
              ingredient={ingredient}
              isExpanded={expandedId === ingredient.id}
              onToggle={() =>
                setExpandedId((current) =>
                  current === ingredient.id ? null : ingredient.id,
                )
              }
              isAdmin={user?.is_admin ?? false}
              listQueryKey={queryKey}
            />
          ))}
        </ul>
      )}
    </>
  );
}

function StatusChip({ status }: { status: IngredientStatus }) {
  return (
    <span className={`status-chip status-chip--${status}`}>
      {STATUS_LABEL[status]}
    </span>
  );
}

type IngredientRowProps = {
  ingredient: IngredientOut;
  isExpanded: boolean;
  onToggle: () => void;
  isAdmin: boolean;
  listQueryKey: QueryKey;
};

function IngredientRow({
  ingredient,
  isExpanded,
  onToggle,
  isAdmin,
  listQueryKey,
}: IngredientRowProps) {
  const queryClient = useQueryClient();
  const detailId = `catalog-detail-${ingredient.id}`;

  const [densityInput, setDensityInput] = useState(
    ingredient.density_g_per_ml != null ? String(ingredient.density_g_per_ml) : "",
  );
  const [statusInput, setStatusInput] = useState<IngredientStatus>(ingredient.status);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Re-seed the edit fields from the latest server values each time the
  // panel opens, so a save-then-reopen (or a background refetch) doesn't
  // leave stale draft values sitting in the inputs.
  useEffect(() => {
    if (!isExpanded) return;
    setDensityInput(
      ingredient.density_g_per_ml != null ? String(ingredient.density_g_per_ml) : "",
    );
    setStatusInput(ingredient.status);
    setSaveError(null);
  }, [isExpanded, ingredient.density_g_per_ml, ingredient.status]);

  const save = useMutation({
    mutationFn: async (patch: IngredientPatch) => {
      const { data, error } = await api.PATCH(
        "/api/catalog/ingredients/{ingredient_id}",
        {
          params: { path: { ingredient_id: ingredient.id } },
          body: patch,
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setSaveError(null);
      void queryClient.invalidateQueries({ queryKey: listQueryKey });
      toast({ title: `${ingredient.name} updated`, variant: "success" });
    },
    onError: (err) => setSaveError(apiErrorMessage(err, "Could not save changes.")),
  });

  const trimmedDensity = densityInput.trim();
  const parsedDensity = trimmedDensity === "" ? null : Number(trimmedDensity);
  const densityIsValid = trimmedDensity === "" || Number.isFinite(parsedDensity);
  const hasChanges =
    (trimmedDensity === ""
      ? ingredient.density_g_per_ml !== null
      : parsedDensity !== ingredient.density_g_per_ml) ||
    statusInput !== ingredient.status;

  function handleSave() {
    if (!densityIsValid) return;
    save.mutate({
      density_g_per_ml: trimmedDensity === "" ? null : parsedDensity,
      status: statusInput,
    });
  }

  const gramWeightEntries = Object.entries(ingredient.gram_weights).sort(([a], [b]) =>
    a.localeCompare(b),
  );

  return (
    <li className="catalog-row-wrap">
      <button
        type="button"
        className="catalog-row catalog-row--body"
        aria-expanded={isExpanded}
        aria-controls={detailId}
        onClick={onToggle}
      >
        <span className="catalog-col catalog-col--name">{ingredient.name}</span>
        <span className="catalog-col catalog-col--category">{ingredient.category}</span>
        <span className="catalog-col catalog-col--density catalog-density">
          {ingredient.density_g_per_ml != null ? ingredient.density_g_per_ml : "—"}
        </span>
        <span className="catalog-col catalog-col--status">
          <StatusChip status={ingredient.status} />
        </span>
        <span className="catalog-col catalog-col--flags">
          {ingredient.dietary_flags.length > 0
            ? ingredient.dietary_flags.join(", ")
            : "—"}
        </span>
      </button>

      {isExpanded ? (
        <div id={detailId} className="catalog-detail">
          <div className="catalog-detail__section">
            <h3 className="catalog-detail__heading">Aliases</h3>
            {ingredient.aliases.length > 0 ? (
              <ul className="catalog-detail__chips">
                {ingredient.aliases.map((alias) => (
                  <li key={`${alias.alias}-${alias.language}`} className="chip chip--static">
                    <span className="chip__text">{alias.alias}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="catalog-detail__muted">No known aliases.</p>
            )}
          </div>

          <div className="catalog-detail__section">
            <h3 className="catalog-detail__heading">Gram weights</h3>
            {gramWeightEntries.length > 0 ? (
              <dl className="catalog-detail__dl">
                {gramWeightEntries.map(([unit, grams]) => (
                  <div key={unit} className="catalog-detail__dl-row">
                    <dt>{unit}</dt>
                    <dd className="catalog-density">{grams} g</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="catalog-detail__muted">No measured conversions yet.</p>
            )}
          </div>

          <div className="catalog-detail__section">
            <h3 className="catalog-detail__heading">Density &amp; status</h3>
            {/* Merge-duplicate-into UI (POST .../merge) is out of scope for
                this pass — admins can only edit density/status here. */}
            {isAdmin ? (
              <div className="catalog-edit">
                <label className="field">
                  <span className="field__label">Density (g/ml)</span>
                  <input
                    type="number"
                    inputMode="decimal"
                    step="0.001"
                    min="0"
                    className="input"
                    value={densityInput}
                    onChange={(event) => setDensityInput(event.target.value)}
                    placeholder="—"
                  />
                </label>
                <label className="field">
                  <span className="field__label">Status</span>
                  <select
                    className="input"
                    value={statusInput}
                    onChange={(event) =>
                      setStatusInput(event.target.value as IngredientStatus)
                    }
                  >
                    {STATUS_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {STATUS_LABEL[option]}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="catalog-edit__actions">
                  <Button
                    variant="secondary"
                    disabled={!hasChanges || !densityIsValid || save.isPending}
                    onClick={handleSave}
                  >
                    {save.isPending ? "Saving…" : "Save"}
                  </Button>
                  {!densityIsValid ? (
                    <span className="field__error">Enter a valid number.</span>
                  ) : null}
                  {saveError ? <span className="field__error">{saveError}</span> : null}
                </div>
              </div>
            ) : (
              <div className="catalog-readonly">
                <p>
                  Density:{" "}
                  <span className="catalog-density">
                    {ingredient.density_g_per_ml != null
                      ? `${ingredient.density_g_per_ml} g/ml`
                      : "—"}
                  </span>
                </p>
                <p>
                  Status: <StatusChip status={ingredient.status} />
                </p>
                <p className="catalog-detail__muted">Curated by admins.</p>
              </div>
            )}
          </div>
        </div>
      ) : null}
    </li>
  );
}
