import { newStep } from "./draft";
import type { StepDraft } from "./draft";
import { RowControls } from "./RowControls";

type StepsEditorProps = {
  steps: StepDraft[];
  onChange: (steps: StepDraft[]) => void;
};

function moveItem<T>(items: T[], index: number, delta: -1 | 1): T[] {
  const target = index + delta;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  return next;
}

export function StepsEditor({ steps, onChange }: StepsEditorProps) {
  function update(stepId: string, text: string) {
    onChange(
      steps.map((step) => (step.id === stepId ? { ...step, text } : step)),
    );
  }

  return (
    <div className="steps">
      <ol className="steps__list">
        {steps.map((step, index) => (
          <li className="step" key={step.id}>
            <span className="step__num" aria-hidden="true">
              {index + 1}.
            </span>
            <textarea
              className="textarea step__text"
              rows={2}
              value={step.text}
              aria-label={`Step ${index + 1}`}
              placeholder={
                index === 0
                  ? "What happens first — e.g. Cream the butter and sugar."
                  : "Then…"
              }
              onChange={(event) => update(step.id, event.target.value)}
            />
            <RowControls
              noun="step"
              upDisabled={index === 0}
              downDisabled={index === steps.length - 1}
              removeDisabled={steps.length === 1}
              onUp={() => onChange(moveItem(steps, index, -1))}
              onDown={() => onChange(moveItem(steps, index, 1))}
              onRemove={() => onChange(steps.filter((s) => s.id !== step.id))}
            />
          </li>
        ))}
      </ol>
      <button
        type="button"
        className="editor-add"
        onClick={() => onChange([...steps, newStep()])}
      >
        + Add step
      </button>
    </div>
  );
}
