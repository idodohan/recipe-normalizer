import { useState } from "react";
import type { components } from "../../api/schema";
import {
  buildRecipeIn,
  newGroup,
  newStep,
  validateDraft,
  type DraftErrors,
  type GroupDraft,
  type RecipeIn,
  type StepDraft,
} from "./draft";

type RecipeOut = components["schemas"]["RecipeOut"];

/* ------------------------------------------------------------------ */
/*  Shared recipe-editing state: used by the manual editor (create)    */
/*  and the extraction review screen (edit a draft), so both stay in    */
/*  lock-step. The rendering lives in RecipeForm.tsx.                   */
/* ------------------------------------------------------------------ */

export type RecipeFormState = {
  title: string;
  description: string;
  servingsAmount: string;
  servingsUnit: string;
  prepMin: string;
  cookMin: string;
  totalMin: string;
  cuisines: string[];
  dishTypes: string[];
  tags: string[];
  groups: GroupDraft[];
  steps: StepDraft[];
};

export function emptyFormState(): RecipeFormState {
  return {
    title: "",
    description: "",
    servingsAmount: "",
    servingsUnit: "",
    prepMin: "",
    cookMin: "",
    totalMin: "",
    cuisines: [],
    dishTypes: [],
    tags: [],
    groups: [newGroup()],
    steps: [newStep()],
  };
}

const numStr = (value: number | null | undefined): string =>
  value === null || value === undefined ? "" : String(value);

/** Seed editing state from a loaded recipe (the extraction draft). */
export function recipeToFormState(recipe: RecipeOut): RecipeFormState {
  const groups: GroupDraft[] = recipe.groups.map((group) => ({
    id: crypto.randomUUID(),
    name: group.name ?? "",
    lines: group.lines.map((line) => ({
      id: crypto.randomUUID(),
      original_text: line.original_text,
      name: line.name ?? "",
      quantity: numStr(line.quantity),
      unit: line.unit ?? "",
      note: line.note ?? "",
      is_optional: line.is_optional,
    })),
  }));
  const steps: StepDraft[] = recipe.steps.map((step) => ({
    id: crypto.randomUUID(),
    text: step.original_text,
  }));
  return {
    title: recipe.title,
    description: recipe.description ?? "",
    servingsAmount: numStr(recipe.servings?.amount),
    servingsUnit: recipe.servings?.unit_text ?? "",
    prepMin: numStr(recipe.prep_min),
    cookMin: numStr(recipe.cook_min),
    totalMin: numStr(recipe.total_min),
    cuisines: recipe.cuisines,
    dishTypes: recipe.dish_types,
    tags: recipe.tags,
    groups: groups.length > 0 ? groups : [newGroup()],
    steps: steps.length > 0 ? steps : [newStep()],
  };
}

export type RecipeFormController = RecipeFormState & {
  set: <K extends keyof RecipeFormState>(key: K, value: RecipeFormState[K]) => void;
  errors: DraftErrors;
  clearError: (key: keyof DraftErrors) => void;
  /** Validate + store errors; returns true when the draft is submittable. */
  validate: () => boolean;
  build: () => RecipeIn;
};

export function useRecipeForm(initial: RecipeFormState): RecipeFormController {
  const [state, setState] = useState<RecipeFormState>(initial);
  const [errors, setErrors] = useState<DraftErrors>({});

  return {
    ...state,
    set: (key, value) => setState((prev) => ({ ...prev, [key]: value })),
    errors,
    clearError: (key) =>
      setErrors((prev) => (prev[key] ? { ...prev, [key]: undefined } : prev)),
    validate: () => {
      const result = validateDraft(state.title, state.groups);
      setErrors(result);
      return !result.title && !result.ingredients;
    },
    build: () => buildRecipeIn(state),
  };
}
