import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { ErrorState } from "../components/ErrorState";
import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { RecipeFormFields } from "../components/recipe/RecipeForm";
import { buildRecipeIn } from "../components/recipe/draft";
import { recipeToFormState, useRecipeForm } from "../components/recipe/recipeFormState";
import { useVocab } from "../hooks/useVocab";
import { toast } from "../hooks/useToast";
import type { components } from "../api/schema";
import "../components/recipe/recipe-editor.css";

type RecipeOut = components["schemas"]["RecipeOut"];

/**
 * Load a saved recipe and hand it to the shared editor form. Split into a
 * loader (this component) and the form (below) so `useRecipeForm` only
 * mounts once the recipe has actually loaded — mirrors how ReviewPage keys
 * DraftEditor by the loaded recipe's id.
 */
export function RecipeEditPage() {
  const { id = "" } = useParams<{ id: string }>();

  const recipe = useQuery({
    queryKey: ["recipe", id],
    enabled: Boolean(id),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes/{recipe_id}", {
        params: { path: { recipe_id: id } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (recipe.isPending) {
    return <Skeleton variant="detail" />;
  }

  if (recipe.isError || !recipe.data) {
    return (
      <ErrorState
        message={apiErrorMessage(recipe.error, "We couldn’t open this recipe.")}
        onRetry={() => void recipe.refetch()}
      />
    );
  }

  return <RecipeEditForm key={recipe.data.id} recipe={recipe.data} />;
}

function RecipeEditForm({ recipe }: { recipe: RecipeOut }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const vocab = useVocab();

  const initialState = recipeToFormState(recipe);
  const form = useRecipeForm(initialState);
  const [banner, setBanner] = useState<string | null>(null);

  // Serialized once from the untouched initial state; compared against the
  // live build() on Cancel to decide whether to confirm discarding.
  const initialPayload = JSON.stringify(buildRecipeIn(initialState));

  const save = useMutation({
    mutationFn: async () => {
      if (!form.validate()) throw new Error("validation");
      const { error } = await api.PATCH("/api/recipes/{recipe_id}", {
        params: { path: { recipe_id: recipe.id } },
        body: form.build(),
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setBanner(null);
      void queryClient.invalidateQueries({ queryKey: ["recipe", recipe.id] });
      void queryClient.invalidateQueries({ queryKey: ["recipes"] });
      toast({ title: "Recipe updated", variant: "success" });
      navigate(`/recipes/${recipe.id}`);
    },
    onError: (err) =>
      err instanceof Error && err.message === "validation"
        ? setBanner("Fix the highlighted fields before saving.")
        : setBanner(apiErrorMessage(err, "Could not save your edits.")),
  });

  function onCancel() {
    const dirty = JSON.stringify(form.build()) !== initialPayload;
    if (dirty && !window.confirm("Discard your changes?")) return;
    navigate(`/recipes/${recipe.id}`);
  }

  return (
    <>
      <PageHeader
        overline="Edit recipe"
        title={recipe.title}
        subtitle="Update the details below — changes apply to this saved recipe."
      />

      <form
        className="editor"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
        noValidate
      >
        {banner ? (
          <div className="editor-banner" role="alert">
            <p className="editor-banner__message">{banner}</p>
          </div>
        ) : null}

        <RecipeFormFields form={form} vocab={vocab.data} />

        <div className="editor__submit-bar">
          <p className="editor__submit-hint">
            A title and one ingredient line are all that’s required.
          </p>
          <div className="editor__submit-actions">
            <Button
              variant="secondary"
              disabled={save.isPending}
              onClick={onCancel}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save changes"}
            </Button>
          </div>
        </div>
      </form>
    </>
  );
}
