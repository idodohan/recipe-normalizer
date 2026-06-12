import type { components } from "../../api/schema";

export type RecipeIn = components["schemas"]["RecipeIn"];

/* ------------------------------------------------------------------ */
/*  Draft model — local editing state with stable ids per row.         */
/* ------------------------------------------------------------------ */

export type LineDraft = {
  id: string;
  original_text: string;
  name: string;
  quantity: string;
  unit: string;
  note: string;
  is_optional: boolean;
};

export type GroupDraft = {
  id: string;
  name: string;
  lines: LineDraft[];
};

export type StepDraft = {
  id: string;
  text: string;
};

export function newLine(): LineDraft {
  return {
    id: crypto.randomUUID(),
    original_text: "",
    name: "",
    quantity: "",
    unit: "",
    note: "",
    is_optional: false,
  };
}

export function newGroup(): GroupDraft {
  return { id: crypto.randomUUID(), name: "", lines: [newLine()] };
}

export function newStep(): StepDraft {
  return { id: crypto.randomUUID(), text: "" };
}

/* ------------------------------------------------------------------ */
/*  Validation + serialization                                         */
/* ------------------------------------------------------------------ */

export type DraftErrors = {
  title?: string;
  ingredients?: string;
};

export function validateDraft(
  title: string,
  groups: GroupDraft[],
): DraftErrors {
  const errors: DraftErrors = {};
  if (!title.trim()) {
    errors.title = "Give the recipe a title.";
  }
  const hasLine = groups.some((group) =>
    group.lines.some((line) => line.original_text.trim().length > 0),
  );
  if (!hasLine) {
    errors.ingredients = "Add at least one ingredient line.";
  }
  return errors;
}

/** "1.5" -> 1.5; "1/2" stays a string the server can parse. */
function parseQuantity(raw: string): number | string | null {
  const text = raw.trim();
  if (!text) return null;
  const asNumber = Number(text);
  return Number.isFinite(asNumber) ? asNumber : text;
}

function parseIntOrNull(raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;
  const asNumber = Number(text);
  return Number.isFinite(asNumber) ? asNumber : null;
}

function orNull(raw: string): string | null {
  const text = raw.trim();
  return text ? text : null;
}

type BuildInput = {
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

/** Trim everything, drop empty trailing lines/steps and empty groups. */
export function buildRecipeIn(input: BuildInput): RecipeIn {
  const groups = input.groups
    .map((group) => ({
      name: orNull(group.name),
      lines: group.lines
        .filter((line) => line.original_text.trim().length > 0)
        .map((line) => ({
          original_text: line.original_text.trim(),
          name: orNull(line.name),
          quantity: parseQuantity(line.quantity),
          unit: orNull(line.unit),
          note: orNull(line.note),
          is_optional: line.is_optional,
        })),
    }))
    .filter((group) => group.lines.length > 0);

  const steps = input.steps
    .map((step) => step.text.trim())
    .filter((text) => text.length > 0)
    .map((text) => ({ original_text: text }));

  const servingsAmount = parseIntOrNull(input.servingsAmount);
  const servingsUnit = orNull(input.servingsUnit);
  const servings =
    servingsAmount !== null || servingsUnit !== null
      ? { amount: servingsAmount, unit_text: servingsUnit }
      : null;

  return {
    title: input.title.trim(),
    description: orNull(input.description),
    language: "en",
    servings,
    prep_min: parseIntOrNull(input.prepMin),
    cook_min: parseIntOrNull(input.cookMin),
    total_min: parseIntOrNull(input.totalMin),
    cuisines: input.cuisines,
    dish_types: input.dishTypes,
    tags: input.tags,
    groups,
    steps,
  };
}
