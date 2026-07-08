import { Button } from "./Button";
import { EmptyState } from "./EmptyState";

type ErrorStateProps = {
  message: string;
  onRetry?: () => void;
};

/** Dead-end error block, styled like EmptyState, with an optional retry. */
export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div role="alert">
      <EmptyState
        title="Something went wrong"
        body={message}
        action={
          onRetry ? (
            <Button variant="secondary" onClick={onRetry}>
              Try again
            </Button>
          ) : undefined
        }
      />
    </div>
  );
}
