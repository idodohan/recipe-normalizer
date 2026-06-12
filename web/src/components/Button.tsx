import type { ButtonHTMLAttributes } from "react";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost";
  block?: boolean;
};

export function Button({
  variant = "primary",
  block = false,
  className,
  ...rest
}: ButtonProps) {
  const classes = ["btn", `btn--${variant}`];
  if (block) classes.push("btn--block");
  if (className) classes.push(className);
  return <button type="button" {...rest} className={classes.join(" ")} />;
}
