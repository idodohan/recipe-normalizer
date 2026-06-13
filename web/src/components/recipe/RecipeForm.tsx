import type { ReactNode } from "react";
import { Field, Input } from "../Field";
import { IngredientGroupsEditor } from "./IngredientGroupsEditor";
import { StepsEditor } from "./StepsEditor";
import { VocabMultiSelect } from "./VocabMultiSelect";
import type { RecipeFormController } from "./recipeFormState";

type Vocab = Record<string, string[]>;

/* ------------------------------------------------------------------ */
/*  Shared recipe-editing form rendering. State lives in recipeForm.ts. */
/* ------------------------------------------------------------------ */

export function EditorSection({
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

export function RecipeFormFields({
  form,
  vocab,
}: {
  form: RecipeFormController;
  vocab: Vocab | undefined;
}) {
  return (
    <>
      <EditorSection
        index="01"
        title="Essentials"
        note="The headline, the yield, and how long it takes."
      >
        <div className="field">
          <input
            className={
              form.errors.title
                ? "editor-title editor-title--invalid"
                : "editor-title"
            }
            type="text"
            value={form.title}
            aria-label="Recipe title"
            aria-invalid={Boolean(form.errors.title)}
            placeholder="Name the recipe…"
            onChange={(event) => {
              form.set("title", event.target.value);
              form.clearError("title");
            }}
          />
          {form.errors.title ? (
            <p className="field__error">{form.errors.title}</p>
          ) : null}
        </div>

        <Field label="Description">
          {(props) => (
            <textarea
              {...props}
              className="textarea"
              rows={3}
              value={form.description}
              placeholder="A line or two about where this comes from, or why it works."
              onChange={(event) => form.set("description", event.target.value)}
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
                  value={form.servingsAmount}
                  placeholder="4"
                  onChange={(event) =>
                    form.set("servingsAmount", event.target.value)
                  }
                />
              )}
            </Field>
            <Field label="As">
              {(props) => (
                <Input
                  {...props}
                  type="text"
                  value={form.servingsUnit}
                  placeholder="servings, loaf (~900g)…"
                  onChange={(event) =>
                    form.set("servingsUnit", event.target.value)
                  }
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
                  type="number"
                  min={0}
                  value={form.prepMin}
                  placeholder="—"
                  onChange={(event) => form.set("prepMin", event.target.value)}
                />
              )}
            </Field>
            <Field label="Cook min">
              {(props) => (
                <Input
                  {...props}
                  className="input--num"
                  type="number"
                  min={0}
                  value={form.cookMin}
                  placeholder="—"
                  onChange={(event) => form.set("cookMin", event.target.value)}
                />
              )}
            </Field>
            <Field label="Total min">
              {(props) => (
                <Input
                  {...props}
                  className="input--num"
                  type="number"
                  min={0}
                  value={form.totalMin}
                  placeholder="—"
                  onChange={(event) => form.set("totalMin", event.target.value)}
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
          values={form.cuisines}
          options={vocab?.cuisines ?? []}
          onChange={(values) => form.set("cuisines", values)}
          placeholder="italian, levantine…"
        />
        <VocabMultiSelect
          label="Dish types"
          values={form.dishTypes}
          options={vocab?.dish_types ?? []}
          onChange={(values) => form.set("dishTypes", values)}
          placeholder="main, dessert, cocktail…"
        />
        <VocabMultiSelect
          label="Tags"
          values={form.tags}
          options={vocab?.tags ?? []}
          onChange={(values) => form.set("tags", values)}
          placeholder="weeknight, make-ahead…"
        />
      </EditorSection>

      <EditorSection
        index="03"
        title="Ingredients"
        note="One line per ingredient, as the source wrote it. The smaller fields help us match and scale; Enter starts the next line."
      >
        {form.errors.ingredients ? (
          <p className="field__error" role="alert">
            {form.errors.ingredients}
          </p>
        ) : null}
        <IngredientGroupsEditor
          groups={form.groups}
          onChange={(groups) => form.set("groups", groups)}
        />
      </EditorSection>

      <EditorSection
        index="04"
        title="Method"
        note="The steps, in order. We keep your wording exactly."
      >
        <StepsEditor
          steps={form.steps}
          onChange={(steps) => form.set("steps", steps)}
        />
      </EditorSection>
    </>
  );
}
