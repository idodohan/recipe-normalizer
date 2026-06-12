/**
 * StepList — the method column. Fraunces oldstyle italic numerals in the
 * margin (matching the editor's hand), step text at a comfortable measure.
 */

type Step = {
  id: string;
  original_text: string;
};

export function StepList({ steps }: { steps: Step[] }) {
  return (
    <ol className="method__steps">
      {steps.map((step, index) => (
        <li key={step.id} className="method-step">
          <span className="method-step__num" aria-hidden="true">
            {index + 1}
          </span>
          <p className="method-step__text">{step.original_text}</p>
        </li>
      ))}
    </ol>
  );
}
