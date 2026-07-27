import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useBlocker, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { ErrorState } from "../components/ErrorState";
import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { RecipeFormFields } from "../components/recipe/RecipeForm";
import { RecipeImageBanner } from "../components/recipe/RecipeImageBanner";
import { buildRecipeIn } from "../components/recipe/draft";
import {
  blockImplicitSubmit,
  recipeToFormState,
  useRecipeForm,
} from "../components/recipe/recipeFormState";
import { useUser } from "../hooks/useUser";
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
  const { user } = useUser();

  // Memoized: `recipeToFormState` mints a fresh uuid per line and per step,
  // so re-running it on every keystroke was pure waste (useRecipeForm only
  // ever reads it as the initial state). Same for its serialization below.
  const initialState = useMemo(() => recipeToFormState(recipe), [recipe]);
  const form = useRecipeForm(initialState);
  const [banner, setBanner] = useState<string | null>(null);

  // Serialized once from the untouched initial state; compared against the
  // live build() to decide whether leaving needs a confirm.
  const initialPayload = useMemo(
    () => JSON.stringify(buildRecipeIn(initialState)),
    [initialState],
  );

  // A successful save navigates away with the form still "dirty" against its
  // initial state — a ref (read when the blocker actually fires, not at
  // render time) is what keeps that navigation from prompting.
  const savedRef = useRef(false);

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
      savedRef.current = true;
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

  const dirty = JSON.stringify(form.build()) !== initialPayload;

  // Every way out of the editor is guarded, not just Cancel: in-app links
  // and the back button go through the router blocker, a reload or a closed
  // tab through beforeunload.
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty &&
      !savedRef.current &&
      currentLocation.pathname !== nextLocation.pathname,
  );

  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm("Discard your changes?")) {
      blocker.proceed();
    } else {
      blocker.reset();
    }
  }, [blocker]);

  useEffect(() => {
    if (!dirty) return;
    // Only a cancelled beforeunload gets the browser's own leave prompt;
    // the wording is the browser's, not ours. Setting returnValue is still
    // required for Safari/older engines that ignore preventDefault() alone.
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // The recipe's photo is owner-only on the backend (set_recipe_image /
  // clear_recipe_image never widen to shared-cookbook members), so members
  // editing a shared recipe don't get the photo controls.
  const isOwner = Boolean(user && user.id === recipe.owner_id);

  // The blocker prompts on its own — Cancel just navigates.
  function onCancel() {
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
        onKeyDown={blockImplicitSubmit}
        noValidate
      >
        {banner ? (
          <div className="editor-banner" role="alert">
            <p className="editor-banner__message">{banner}</p>
          </div>
        ) : null}

        <RecipeImageBanner
          recipeId={recipe.id}
          imageUrl={recipe.image_ref ?? null}
          canManage={isOwner}
        />

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
