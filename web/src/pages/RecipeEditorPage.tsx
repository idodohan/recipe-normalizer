import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { Field, Input } from "../components/Field";
import { PageHeader } from "../components/PageHeader";
import {
  buildRecipeIn,
  newGroup,
  newStep,
  validateDraft,
} from "../components/recipe/draft";
import type {
  DraftErrors,
  GroupDraft,
  StepDraft,
} from "../components/recipe/draft";
import { IngredientGroupsEditor } from "../components/recipe/IngredientGroupsEditor";
import { StepsEditor } from "../components/recipe/StepsEditor";
import { VocabMultiSelect } from "../components/recipe/VocabMultiSelect";
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

function EditorSection({
  index,
  title,
  note,
  children,
}: {
  index: string;
  title: string;
  note: string;
  children: ReactNode;
}) {
  return (
    <section className="editor__section">
      <header className="editor__section-head">
        <span className="editor__section-index" aria-hidden="true">
          {index}
        </span>
        <h2 className="editor__section-title">{title}</h2>
        <p className="editor__section-note">{note}</p>
      </header>
      <div className="editor__section-body">{children}</div>
    </section>
  );
}

export function RecipeEditorPage() {
  const navigate = useNavigate();

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [servingsAmount, setServingsAmount] = useState("");
  const [servingsUnit, setServingsUnit] = useState("");
  const [prepMin, setPrepMin] = useState("");
  const [cookMin, setCookMin] = useState("");
  const [totalMin, setTotalMin] = useState("");
  const [cuisines, setCuisines] = useState<string[]>([]);
  const [dishTypes, setDishTypes] = useState<string[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [groups, setGroups] = useState<GroupDraft[]>(() => [newGroup()]);
  const [steps, setSteps] = useState<StepDraft[]>(() => [newStep()]);

  const [errors, setErrors] = useState<DraftErrors>({});
  const [banner, setBanner] = useState<Banner | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const vocab = useQuery({
    queryKey: ["vocab"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/vocab");
      if (error) throw error;
      return data;
    },
  });

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBanner(null);

    const validation = validateDraft(title, groups);
    setErrors(validation);
    if (validation.title || validation.ingredients) return;

    setSubmitting(true);
    try {
      const body = buildRecipeIn({
        title,
        description,
        servingsAmount,
        servingsUnit,
        prepMin,
        cookMin,
        totalMin,
        cuisines,
        dishTypes,
        tags,
        groups,
        steps,
      });
      const { data, error, response } = await api.POST("/api/recipes", {
        body,
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

        <EditorSection
          index="01"
          title="Essentials"
          note="The headline, the yield, and how long it takes."
        >
          <div className="field">
            <input
              className={
                errors.title ? "editor-title editor-title--invalid" : "editor-title"
              }
              type="text"
              value={title}
              aria-label="Recipe title"
              aria-invalid={Boolean(errors.title)}
              placeholder="Name the recipe…"
              onChange={(event) => {
                setTitle(event.target.value);
                if (errors.title) setErrors({ ...errors, title: undefined });
              }}
            />
            {errors.title ? (
              <p className="field__error">{errors.title}</p>
            ) : null}
          </div>

          <Field label="Description">
            {(props) => (
              <textarea
                {...props}
                className="textarea"
                rows={3}
                value={description}
                placeholder="A line or two about where this comes from, or why it works."
                onChange={(event) => setDescription(event.target.value)}
              />
            )}
          </Field>

          <div className="editor__essentials-row">
            <div className="editor__servings">
              <Field label="Serves">
                {(props) => (
                  <Input
                    {...props}
                    className="input--num"
                    type="text"
                    inputMode="numeric"
                    value={servingsAmount}
                    placeholder="4"
                    onChange={(event) => setServingsAmount(event.target.value)}
                  />
                )}
              </Field>
              <Field label="As">
                {(props) => (
                  <Input
                    {...props}
                    type="text"
                    value={servingsUnit}
                    placeholder="servings, loaf (~900g)…"
                    onChange={(event) => setServingsUnit(event.target.value)}
                  />
                )}
              </Field>
            </div>
            <div className="editor__times">
              <Field label="Prep min">
                {(props) => (
                  <Input
                    {...props}
                    className="input--num"
                    type="text"
                    inputMode="numeric"
                    value={prepMin}
                    placeholder="—"
                    onChange={(event) => setPrepMin(event.target.value)}
                  />
                )}
              </Field>
              <Field label="Cook min">
                {(props) => (
                  <Input
                    {...props}
                    className="input--num"
                    type="text"
                    inputMode="numeric"
                    value={cookMin}
                    placeholder="—"
                    onChange={(event) => setCookMin(event.target.value)}
                  />
                )}
              </Field>
              <Field label="Total min">
                {(props) => (
                  <Input
                    {...props}
                    className="input--num"
                    type="text"
                    inputMode="numeric"
                    value={totalMin}
                    placeholder="—"
                    onChange={(event) => setTotalMin(event.target.value)}
                  />
                )}
              </Field>
            </div>
          </div>
        </EditorSection>

        <EditorSection
          index="02"
          title="Classification"
          note="Pick from the shared vocabulary, or coin a new term — Enter adds it."
        >
          <VocabMultiSelect
            label="Cuisines"
            values={cuisines}
            options={vocab.data?.cuisines ?? []}
            onChange={setCuisines}
            placeholder="italian, levantine…"
          />
          <VocabMultiSelect
            label="Dish types"
            values={dishTypes}
            options={vocab.data?.dish_types ?? []}
            onChange={setDishTypes}
            placeholder="main, dessert, cocktail…"
          />
          <VocabMultiSelect
            label="Tags"
            values={tags}
            options={vocab.data?.tags ?? []}
            onChange={setTags}
            placeholder="weeknight, make-ahead…"
          />
        </EditorSection>

        <EditorSection
          index="03"
          title="Ingredients"
          note="One line per ingredient, as the source wrote it. The smaller fields help us match and scale; Enter starts the next line."
        >
          {errors.ingredients ? (
            <p className="field__error" role="alert">
              {errors.ingredients}
            </p>
          ) : null}
          <IngredientGroupsEditor groups={groups} onChange={setGroups} />
        </EditorSection>

        <EditorSection
          index="04"
          title="Method"
          note="The steps, in order. We keep your wording exactly."
        >
          <StepsEditor steps={steps} onChange={setSteps} />
        </EditorSection>

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
