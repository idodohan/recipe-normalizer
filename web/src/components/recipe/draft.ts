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

/** Move an item up/down within a list (used by lines, groups, and steps). */
export function moveItem<T>(items: T[], index: number, delta: -1 | 1): T[] {
  const target = index + delta;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  return next;
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

/**
 * Parse a typed quantity to a number the API accepts (Decimal, > 0):
 * decimals ("1.5"), simple fractions ("1/2"), and mixed numbers ("1 1/2").
 * Anything else -> null; original_text still carries the user's words, so
 * the dual-quantity rule keeps them visible.
 */
export function parseQuantity(raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;

  let value: number;
  const mixed = /^(\d+)\s+(\d+)\s*\/\s*(\d+)$/.exec(text);
  const fraction = /^(\d+)\s*\/\s*(\d+)$/.exec(text);
  if (mixed) {
    const denominator = Number(mixed[3]);
    if (denominator === 0) return null;
    value = Number(mixed[1]) + Number(mixed[2]) / denominator;
  } else if (fraction) {
    const denominator = Number(fraction[2]);
    if (denominator === 0) return null;
    value = Number(fraction[1]) / denominator;
  } else {
    value = Number(text);
  }
  // The API requires quantity > 0.
  return Number.isFinite(value) && value > 0 ? value : null;
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
