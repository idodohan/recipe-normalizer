import { useId } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";

type InputProps = InputHTMLAttributes<HTMLInputElement> & {
  invalid?: boolean;
};

export function Input({ invalid = false, className, ...rest }: InputProps) {
  const classes = ["input"];
  if (invalid) classes.push("input--invalid");
  if (className) classes.push(className);
  return <input {...rest} className={classes.join(" ")} />;
}

type FieldProps = {
  label: string;
  error?: ReactNode;
  children: (props: {
    id: string;
    "aria-invalid": boolean;
    "aria-describedby": string | undefined;
  }) => ReactNode;
};

/** Label-above field wrapper with an error slot. */
export function Field({ label, error, children }: FieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {children({
        id,
        "aria-invalid": Boolean(error),
        "aria-describedby": error ? errorId : undefined,
      })}
      {error ? (
        <p className="field__error" id={errorId}>
          {error}
        </p>
      ) : null}
    </div>
  );
}
