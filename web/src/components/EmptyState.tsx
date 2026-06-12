import type { ReactNode } from "react";

type EmptyStateProps = {
  title: string;
  body?: ReactNode;
  action?: ReactNode;
};

export function EmptyState({ title, body, action }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <div className="empty-state__rule" aria-hidden="true" />
      <h2 className="empty-state__title">{title}</h2>
      {body ? <p className="empty-state__body">{body}</p> : null}
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  );
}
