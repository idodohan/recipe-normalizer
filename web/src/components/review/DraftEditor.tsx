import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { apiErrorMessage } from "../../api/errors";
import { Button } from "../Button";
import { RecipeFormFields } from "../recipe/RecipeForm";
import { recipeToFormState, useRecipeForm } from "../recipe/recipeFormState";
import type { components } from "../../api/schema";

type RecipeOut = components["schemas"]["RecipeOut"];

/**
 * One draft's editable form + review actions. Mounted with key={recipe.id} so
 * switching drafts re-seeds the form from the freshly loaded recipe.
 */
export function DraftEditor({
  jobId,
  recipe,
  confidence,
  onResolved,
}: {
  jobId: string;
  recipe: RecipeOut;
  confidence: number | null;
  onResolved: () => void;
}) {
  const queryClient = useQueryClient();
  const form = useRecipeForm(recipeToFormState(recipe));
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [confirmingReject, setConfirmingReject] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const vocab = useQuery({
    queryKey: ["vocab"],
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error: vocabError } = await api.GET("/api/vocab");
      if (vocabError) throw vocabError;
      return data;
    },
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    void queryClient.invalidateQueries({ queryKey: ["recipes"] });
  };

  const save = useMutation({
    mutationFn: async () => {
      if (!form.validate()) throw new Error("validation");
      const { error: patchError } = await api.PATCH("/api/recipes/{recipe_id}", {
        params: { path: { recipe_id: recipe.id } },
        body: form.build(),
      });
      if (patchError) throw patchError;
    },
    onSuccess: () => {
      setError(null);
      setSavedAt(Date.now());
      void queryClient.invalidateQueries({ queryKey: ["recipe", recipe.id] });
    },
    onError: (err) =>
      err instanceof Error && err.message === "validation"
        ? setError("Fix the highlighted fields before saving.")
        : setError(apiErrorMessage(err, "Could not save your edits.")),
  });

  const accept = useMutation({
    mutationFn: async () => {
      // Persist edits first so the accepted recipe reflects the review.
      if (!form.validate()) throw new Error("validation");
      const patch = await api.PATCH("/api/recipes/{recipe_id}", {
        params: { path: { recipe_id: recipe.id } },
        body: form.build(),
      });
      if (patch.error) throw patch.error;
      const { error: acceptError } = await api.POST(
        "/api/jobs/{job_id}/recipes/{recipe_id}/accept",
        { params: { path: { job_id: jobId, recipe_id: recipe.id } } },
      );
      if (acceptError) throw acceptError;
    },
    onSuccess: () => {
      invalidate();
      onResolved();
    },
    onError: (err) =>
      err instanceof Error && err.message === "validation"
        ? setError("Fix the highlighted fields before accepting.")
        : setError(apiErrorMessage(err, "Could not accept this draft.")),
  });

  const reject = useMutation({
    mutationFn: async () => {
      const { error: rejectError } = await api.POST(
        "/api/jobs/{job_id}/recipes/{recipe_id}/reject",
        { params: { path: { job_id: jobId, recipe_id: recipe.id } } },
      );
      if (rejectError) throw rejectError;
    },
    onSuccess: () => {
      invalidate();
      onResolved();
    },
    onError: (err) => setError(apiErrorMessage(err, "Could not reject this draft.")),
  });

  const busy = save.isPending || accept.isPending || reject.isPending;
  const lowConfidence = confidence !== null && confidence < 0.7;

  return (
    <div className="draft-editor">
      {confidence !== null ? (
        <p
          className={
            lowConfidence
              ? "draft-confidence draft-confidence--low"
              : "draft-confidence"
          }
        >
          <span className="draft-confidence__value">
            {Math.round(confidence * 100)}% confidence
          </span>
          {lowConfidence
            ? " — review carefully; the extractor was unsure here."
            : " — looks clean, but give it a read."}
        </p>
      ) : null}

      {error ? (
        <div className="draft-editor__error" role="alert">
          {error}
        </div>
      ) : null}

      <div className="editor">
        <RecipeFormFields form={form} vocab={vocab.data} />
      </div>

      <div className="draft-actions">
        {confirmingReject ? (
          <div className="draft-actions__confirm">
            <span>Discard this draft?</span>
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => setConfirmingReject(false)}
            >
              Keep
            </Button>
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => reject.mutate()}
            >
              {reject.isPending ? "Discarding…" : "Discard"}
            </Button>
          </div>
        ) : (
          <>
            <button
              type="button"
              className="btn btn--ghost draft-actions__reject"
              disabled={busy}
              onClick={() => setConfirmingReject(true)}
            >
              Reject
            </button>
            <span className="draft-actions__spacer" />
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => save.mutate()}
            >
              {save.isPending
                ? "Saving…"
                : savedAt
                  ? "Saved ✓"
                  : "Save edits"}
            </Button>
            <Button disabled={busy} onClick={() => accept.mutate()}>
              {accept.isPending ? "Accepting…" : "Accept recipe"}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
