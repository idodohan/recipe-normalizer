import { useState } from "react";
import type { KeyboardEvent } from "react";

/** What the cook asked for. `null` means the unscaled recipe (no fetch). */
export type ScaleRequest =
  | { kind: "factor"; factor: number }
  | { kind: "servings"; servings: number };

const PRESETS: Array<{ label: string; factor: number }> = [
  { label: "×½", factor: 0.5 },
  { label: "×1", factor: 1 },
  { label: "×2", factor: 2 },
  { label: "×3", factor: 3 },
];

const FACTOR_MIN = 0.1;
const FACTOR_MAX = 100;

type ScaleControlProps = {
  scale: ScaleRequest | null;
  onChange: (next: ScaleRequest | null) => void;
  /** `recipe.servings?.amount` — enables the "for N servings" input. */
  baseServings?: number | null;
};

/**
 * The scaling instrument strip: presets, a free factor, and — when the
 * recipe declares servings — a target-servings input. ×1 (or clearing)
 * returns to the unscaled recipe without fetching.
 */
export function ScaleControl({ scale, onChange, baseServings }: ScaleControlProps) {
  const [factorText, setFactorText] = useState("1");
  const [servingsText, setServingsText] = useState("");

  // Keep the inputs honest whenever the applied scale changes (state
  // adjusted during render, per React's recommended pattern).
  const [prevScale, setPrevScale] = useState(scale);
  if (prevScale !== scale) {
    setPrevScale(scale);
    if (scale?.kind === "factor") {
      setFactorText(String(scale.factor));
      setServingsText("");
    } else if (scale?.kind === "servings") {
      setFactorText("");
      setServingsText(String(scale.servings));
    } else {
      setFactorText("1");
      setServingsText("");
    }
  }

  function commitFactor() {
    const value = Number(factorText);
    if (factorText.trim() === "" || !Number.isFinite(value)) {
      // Revert to whatever is applied.
      setFactorText(scale?.kind === "factor" ? String(scale.factor) : scale ? "" : "1");
      return;
    }
    const clamped = Math.min(FACTOR_MAX, Math.max(FACTOR_MIN, value));
    onChange(clamped === 1 ? null : { kind: "factor", factor: clamped });
    setFactorText(String(clamped));
  }

  function commitServings() {
    const value = Math.round(Number(servingsText));
    if (servingsText.trim() === "" || !Number.isFinite(value) || value < 1) {
      setServingsText(scale?.kind === "servings" ? String(scale.servings) : "");
      return;
    }
    // Asking for the recipe's own yield is the unscaled recipe.
    onChange(value === baseServings ? null : { kind: "servings", servings: value });
    setServingsText(String(value));
  }

  function blurOnEnter(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") event.currentTarget.blur();
  }

  return (
    <div className="scale">
      <span className="scale__label" id="scale-label">
        Scale
      </span>

      <div className="scale__presets" role="group" aria-labelledby="scale-label">
        {PRESETS.map((preset) => {
          const active =
            (scale === null && preset.factor === 1) ||
            (scale?.kind === "factor" && scale.factor === preset.factor);
          return (
            <button
              key={preset.label}
              type="button"
              className={
                active ? "scale__preset scale__preset--active" : "scale__preset"
              }
              aria-pressed={active}
              onClick={() =>
                onChange(
                  preset.factor === 1
                    ? null
                    : { kind: "factor", factor: preset.factor },
                )
              }
            >
              {preset.label}
            </button>
          );
        })}
      </div>

      <label className="scale__free">
        ×
        <input
          className="scale__input"
          type="number"
          min={FACTOR_MIN}
          max={FACTOR_MAX}
          step={0.1}
          value={factorText}
          aria-label="Scale factor"
          onChange={(event) => setFactorText(event.target.value)}
          onBlur={commitFactor}
          onKeyDown={blurOnEnter}
        />
      </label>

      {baseServings ? (
        <label className="scale__servings">
          for
          <input
            className="scale__input scale__input--servings"
            type="number"
            min={1}
            step={1}
            value={servingsText}
            aria-label="Target servings"
            onChange={(event) => setServingsText(event.target.value)}
            onBlur={commitServings}
            onKeyDown={blurOnEnter}
          />
          servings
        </label>
      ) : null}
    </div>
  );
}
