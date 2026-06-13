import { useState } from "react";
import type { FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { PageHeader } from "../components/PageHeader";
import { RecipeFormFields } from "../components/recipe/RecipeForm";
import { emptyFormState, useRecipeForm } from "../components/recipe/recipeFormState";
import "../components/recipe/recipe-editor.css";

type Banner = {
  message: string;
  existingId?: string;
};

/** Pull `existing_id` out of the 409 duplicate-recipe envelope. */
function extractExistingId(body: unknown): string | undefined {
  if (body && typeof body === "object" && "error" in body) {
    const err = (body as { error: unknown }).error;
    if (err && typeof err === "object" && "existing_id" in err) {
      const value = (err as { existing_id: unknown }).existing_id;
      if (typeof value === "string") return value;
    }
  }
  return undefined;
}

export function RecipeEditorPage() {
  const navigate = useNavigate();
  const form = useRecipeForm(emptyFormState());
  const [banner, setBanner] = useState<Banner | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const vocab = useQuery({
    queryKey: ["vocab"],
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/vocab");
      if (error) throw error;
      return data;
    },
  });

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBanner(null);
    if (!form.validate()) return;

    setSubmitting(true);
    try {
      const { data, error, response } = await api.POST("/api/recipes", {
        body: form.build(),
      });
      if (data) {
        navigate(`/recipes/${data.id}`);
        return;
      }
      if (response.status === 409) {
        setBanner({
          message: "You already have this recipe.",
          existingId: extractExistingId(error),
        });
      } else {
        setBanner({
          message: apiErrorMessage(
            error,
            "Could not save the recipe. Please try again.",
          ),
        });
      }
    } catch {
      setBanner({ message: "Could not reach the server. Please try again." });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <PageHeader
        overline="New entry"
        title="Add a recipe"
        subtitle="Write it the way the source does — we’ll take care of the measures."
      />

      <form className="editor" onSubmit={(e) => void onSubmit(e)} noValidate>
        {banner ? (
          <div className="editor-banner" role="alert">
            <p className="editor-banner__message">
              {banner.message}{" "}
              {banner.existingId ? (
                <Link to={`/recipes/${banner.existingId}`}>
                  View the existing recipe
                </Link>
              ) : null}
            </p>
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
              disabled={submitting}
              onClick={() => navigate("/")}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting}>
              {submitting ? "Saving…" : "Save recipe"}
            </Button>
          </div>
        </div>
      </form>
    </>
  );
}
