import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import { apiErrorEnvelope, apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { PageHeader } from "../components/PageHeader";
import { RecipeFormFields } from "../components/recipe/RecipeForm";
import {
  blockImplicitSubmit,
  emptyFormState,
  useRecipeForm,
} from "../components/recipe/recipeFormState";
import { useVocab } from "../hooks/useVocab";
import { toast } from "../hooks/useToast";
import "../components/recipe/recipe-editor.css";

type Banner = {
  message: string;
  existingId?: string;
};

export function RecipeEditorPage() {
  const navigate = useNavigate();
  const form = useRecipeForm(emptyFormState());
  const [banner, setBanner] = useState<Banner | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [pasted, setPasted] = useState("");

  const vocab = useVocab();

  // Paste-first is the primary path: the product exists to parse recipe text,
  // so the default "add" experience is to hand it text and let the extraction
  // pipeline normalize it into a review draft — not to make the user fill in
  // quantity/unit/name for every line by hand. The field-by-field form below
  // stays as the deliberate fallback (and the only path when no LLM is set up).
  const paste = useMutation({
    mutationFn: async (value: string) => {
      const { data, error } = await api.POST("/api/ingest/text", {
        body: { text: value },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      toast({ title: "Normalizing your recipe…", variant: "success" });
      navigate("/inbox");
    },
    onError: (error) =>
      setBanner({
        message: apiErrorMessage(error, "Could not submit. Please try again."),
      }),
  });

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBanner(null);
    if (!form.validate()) {
      // Surface why nothing happened — the invalid fields are marked inline,
      // but a silent no-op reads as a broken button (checker C5).
      setBanner({ message: "Fix the highlighted fields before saving." });
      return;
    }

    setSubmitting(true);
    try {
      const { data, error, response } = await api.POST("/api/recipes", {
        body: form.build(),
      });
      if (data) {
        toast({ title: "Recipe created", variant: "success" });
        navigate(`/recipes/${data.id}`);
        return;
      }
      if (response.status === 409) {
        setBanner({
          message: "You already have this recipe.",
          existingId: apiErrorEnvelope(error).existingId,
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
        subtitle="Paste it and we’ll normalize the amounts — or enter it yourself."
      />

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

      <section className="paste-create" aria-label="Paste a recipe">
        <label className="paste-create__label" htmlFor="paste-recipe">
          Paste your recipe
        </label>
        <p className="paste-create__hint">
          Title, ingredients, and steps — however the source wrote them. We’ll
          parse the ingredient lines, normalize every amount to grams or
          millilitres, and hand you a draft to review before it’s saved.
        </p>
        <textarea
          id="paste-recipe"
          className="paste-create__textarea"
          rows={10}
          value={pasted}
          disabled={paste.isPending}
          onChange={(event) => setPasted(event.target.value)}
          placeholder={
            "Lemon Garlic Chicken\nServes 4\n\n2 cups flour\n1 tsp salt\n4 tbsp butter\n\nMix and bake at 400°F for 30 minutes."
          }
        />
        <div className="paste-create__actions">
          <Button variant="secondary" onClick={() => navigate("/")}>
            Cancel
          </Button>
          <Button
            disabled={paste.isPending || !pasted.trim()}
            onClick={() => {
              setBanner(null);
              paste.mutate(pasted.trim());
            }}
          >
            {paste.isPending ? "Sending…" : "Normalize it"}
          </Button>
        </div>
      </section>

      <details className="manual-entry" open>
        <summary className="manual-entry__summary">
          Prefer to enter it field by field?
        </summary>
        <form
          className="editor"
          onSubmit={(e) => void onSubmit(e)}
          onKeyDown={blockImplicitSubmit}
          noValidate
        >
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
      </details>
    </>
  );
}
