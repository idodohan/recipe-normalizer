import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorEnvelope, apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { Field, Input } from "../components/Field";
import { JobList } from "../components/inbox/JobList";
import { RecipeFormFields } from "../components/recipe/RecipeForm";
import {
  blockImplicitSubmit,
  emptyFormState,
  useRecipeForm,
} from "../components/recipe/recipeFormState";
import { useJobs } from "../hooks/useJobs";
import { useVocab } from "../hooks/useVocab";
import { toast } from "../hooks/useToast";
import "../components/recipe/recipe-editor.css";
import "./add-recipe.css";

/* ------------------------------------------------------------------ */
/*  Source-submission notices — lifted from the old SubmitPanel so the  */
/*  duplicate-recipe / duplicate-job envelopes still surface a link.    */
/* ------------------------------------------------------------------ */

type SourceNotice =
  | { kind: "error"; message: string }
  | { kind: "duplicate-recipe"; message: string; recipeId: string }
  | { kind: "duplicate-job"; message: string; jobId: string };

function readEnvelope(error: unknown): SourceNotice {
  const { code, message, existingId, jobId } = apiErrorEnvelope(error);
  const resolvedMessage = message ?? "Could not submit. Please try again.";
  if (code === "duplicate_recipe" && existingId) {
    return { kind: "duplicate-recipe", message: resolvedMessage, recipeId: existingId };
  }
  if (code === "duplicate_job" && jobId) {
    return { kind: "duplicate-job", message: resolvedMessage, jobId };
  }
  return { kind: "error", message: resolvedMessage };
}

type ManualBanner = { message: string; existingId?: string };

export function AddRecipeWizardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Shares the ["jobs"] cache with JobList (react-query dedupes the request).
  const jobs = useJobs();
  const showStatus = jobs.isError || (jobs.data?.length ?? 0) > 0;

  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<SourceNotice | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const onSettled = () => {
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
  };

  const submitUrl = useMutation({
    mutationFn: async (value: string) => {
      const { data, error } = await api.POST("/api/ingest/url", {
        body: { url: value },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setUrl("");
      setNotice(null);
      onSettled();
      toast({ title: "Added — we're normalizing it", variant: "success" });
    },
    onError: (error) => setNotice(readEnvelope(error)),
  });

  const submitText = useMutation({
    mutationFn: async (value: string) => {
      const { data, error } = await api.POST("/api/ingest/text", {
        body: { text: value },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setText("");
      setNotice(null);
      onSettled();
      toast({ title: "Added — we're normalizing it", variant: "success" });
    },
    onError: (error) => setNotice(readEnvelope(error)),
  });

  const submitFile = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/ingest/file", {
        method: "POST",
        body: form,
        credentials: "include",
      });
      const body = (await response.json().catch(() => null)) as unknown;
      if (!response.ok) throw body ?? { error: { message: "Upload failed." } };
      return body;
    },
    onSuccess: () => {
      setNotice(null);
      onSettled();
      toast({ title: "Added — we're normalizing it", variant: "success" });
    },
    onError: (error) => setNotice(readEnvelope(error)),
  });

  const busy =
    submitUrl.isPending || submitText.isPending || submitFile.isPending;

  /* ---------------------------------------------------------------- */
  /*  Manual "field by field" fallback — lifted from RecipeEditorPage.  */
  /* ---------------------------------------------------------------- */

  const vocab = useVocab();
  const manualForm = useRecipeForm(emptyFormState());
  const [manualOpen, setManualOpen] = useState(false);
  const [manualBanner, setManualBanner] = useState<ManualBanner | null>(null);
  const [manualSubmitting, setManualSubmitting] = useState(false);

  async function onManualSubmit(event: FormEvent) {
    event.preventDefault();
    setManualBanner(null);
    if (!manualForm.validate()) {
      // Surface why nothing happened — the invalid fields are marked inline,
      // but a silent no-op reads as a broken button.
      setManualBanner({ message: "Fix the highlighted fields before saving." });
      return;
    }

    setManualSubmitting(true);
    try {
      const { data, error, response } = await api.POST("/api/recipes", {
        body: manualForm.build(),
      });
      if (data) {
        toast({ title: "Recipe created", variant: "success" });
        navigate(`/recipes/${data.id}`);
        return;
      }
      if (response.status === 409) {
        setManualBanner({
          message: "You already have this recipe.",
          existingId: apiErrorEnvelope(error).existingId,
        });
      } else {
        setManualBanner({
          message: apiErrorMessage(
            error,
            "Could not save the recipe. Please try again.",
          ),
        });
      }
    } catch {
      setManualBanner({ message: "Could not reach the server. Please try again." });
    } finally {
      setManualSubmitting(false);
    }
  }

  return (
    <>
      <header className="add-recipe__masthead">
        <p className="add-recipe__overline">New</p>
        <h1 className="add-recipe__title">Add a recipe</h1>
        <p className="add-recipe__subtitle">
          Paste a link, drop a file, or type it in — we normalize every
          amount and hand you a draft to review.
        </p>
      </header>

      <section className="source-grid" aria-label="Add a recipe source">
        <form
          className="source-card"
          onSubmit={(event) => {
            event.preventDefault();
            if (url.trim()) submitUrl.mutate(url.trim());
          }}
        >
          <h2 className="source-card__title">Paste a link</h2>
          <p className="source-card__hint">A recipe page anywhere on the web.</p>
          <div className="source-card__row">
            <Field label="Recipe URL">
              {(props) => (
                <Input
                  {...props}
                  type="url"
                  inputMode="url"
                  placeholder="https://…"
                  value={url}
                  disabled={busy}
                  onChange={(event) => setUrl(event.target.value)}
                />
              )}
            </Field>
            <Button type="submit" disabled={busy || !url.trim()}>
              Fetch
            </Button>
          </div>
        </form>

        <form
          className="source-card"
          onSubmit={(event) => {
            event.preventDefault();
            if (text.trim()) submitText.mutate(text.trim());
          }}
        >
          <h2 className="source-card__title">Paste text</h2>
          <p className="source-card__hint">
            Title, ingredients, and steps — however the source wrote them.
            Typing it in by hand? Just type it here as plain text.
          </p>
          <Field label="Recipe text">
            {(props) => (
              <textarea
                {...props}
                className="textarea source-card__textarea"
                rows={6}
                value={text}
                disabled={busy}
                placeholder={
                  "Lemon Garlic Chicken\nServes 4\n\n2 cups flour\n1 tsp salt\n4 tbsp butter\n\nMix and bake at 400°F for 30 minutes."
                }
                onChange={(event) => setText(event.target.value)}
              />
            )}
          </Field>
          <Button
            type="submit"
            className="source-card__submit"
            disabled={busy || !text.trim()}
          >
            Add text
          </Button>
        </form>

        <div
          className={
            dragging
              ? "source-card source-card--drop is-dragging"
              : "source-card source-card--drop"
          }
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            const file = event.dataTransfer.files?.[0];
            if (file) submitFile.mutate(file);
          }}
        >
          <h2 className="source-card__title">Upload a PDF or photo</h2>
          <p className="source-card__hint">A scanned page, a menu photo, or a PDF.</p>
          <button
            className="source-card__drop-target"
            type="button"
            disabled={busy}
            onClick={() => fileInput.current?.click()}
          >
            {submitFile.isPending ? "Uploading…" : "Drag here, or browse"}
          </button>
          <input
            ref={fileInput}
            className="source-card__file"
            type="file"
            aria-label="Recipe file"
            accept="application/pdf,image/png,image/jpeg,image/webp,image/gif"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) submitFile.mutate(file);
              event.target.value = "";
            }}
          />
        </div>
      </section>

      {notice ? (
        <p
          className={
            notice.kind === "error"
              ? "source-notice source-notice--error"
              : "source-notice"
          }
          role="status"
        >
          {notice.message}{" "}
          {notice.kind === "duplicate-recipe" ? (
            <Link to={`/recipes/${notice.recipeId}`}>View the recipe →</Link>
          ) : null}
          {notice.kind === "duplicate-job" ? (
            <Link to={`/jobs/${notice.jobId}/review`}>Open the job →</Link>
          ) : null}
        </p>
      ) : null}

      <details
        className="manual-entry"
        open={manualOpen}
        onToggle={(e) => setManualOpen(e.currentTarget.open)}
      >
        <summary className="manual-entry__summary">
          Or enter it field by field
        </summary>
        {/* Rendered only once opened: the full RecipeFormFields is heavy
            (many inputs), so mounting it eagerly would slow every /add visit. */}
        {manualOpen ? (
        <form
          className="editor"
          onSubmit={(e) => void onManualSubmit(e)}
          onKeyDown={blockImplicitSubmit}
          noValidate
        >
          {manualBanner ? (
            <div className="editor-banner" role="alert">
              <p className="editor-banner__message">
                {manualBanner.message}{" "}
                {manualBanner.existingId ? (
                  <Link to={`/recipes/${manualBanner.existingId}`}>
                    View the existing recipe
                  </Link>
                ) : null}
              </p>
            </div>
          ) : null}

          <RecipeFormFields form={manualForm} vocab={vocab.data} />

          <div className="editor__submit-bar">
            <p className="editor__submit-hint">
              A title and one ingredient line are all that’s required.
            </p>
            <div className="editor__submit-actions">
              <Button
                variant="secondary"
                disabled={manualSubmitting}
                onClick={() => navigate("/")}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={manualSubmitting}>
                {manualSubmitting ? "Saving…" : "Save recipe"}
              </Button>
            </div>
          </div>
        </form>
        ) : null}
      </details>

      {showStatus ? (
        <section className="add-recipe__status" aria-live="polite">
          <h2 className="add-recipe__section-title">In progress</h2>
          <JobList />
        </section>
      ) : null}
    </>
  );
}
