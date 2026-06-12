import type { ReactNode } from "react";

type PageHeaderProps = {
  title: string;
  overline?: string;
  subtitle?: ReactNode;
  action?: ReactNode;
};

export function PageHeader({ title, overline, subtitle, action }: PageHeaderProps) {
  return (
    <header className="page-header">
      <div>
        {overline ? <span className="page-header__overline">{overline}</span> : null}
        <h1 className="page-header__title">{title}</h1>
        {subtitle ? <p className="page-header__subtitle">{subtitle}</p> : null}
      </div>
      {action ? <div>{action}</div> : null}
    </header>
  );
}
