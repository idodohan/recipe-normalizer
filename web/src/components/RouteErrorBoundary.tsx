import { isRouteErrorResponse, useRouteError } from "react-router-dom";
import { ErrorState } from "./ErrorState";

/** What to tell the reader, by the shape of what was thrown. */
function readerMessage(error: unknown): string {
  if (isRouteErrorResponse(error)) {
    if (error.status === 404) {
      return "We couldn’t find that page. The link may be stale.";
    }
    return `That page couldn’t be loaded (${error.status}). Reloading may sort it out.`;
  }
  return "This page ran into an unexpected error. Reloading usually sorts it out.";
}

/** The developer-facing detail — shown in dev builds only. */
function errorDetail(error: unknown): string | null {
  if (isRouteErrorResponse(error)) {
    return `${error.status} ${error.statusText}\n${typeof error.data === "string" ? error.data : JSON.stringify(error.data)}`;
  }
  if (error instanceof Error) {
    return error.stack ?? `${error.name}: ${error.message}`;
  }
  if (error == null) return null;
  return String(error);
}

/**
 * The router's `errorElement`. Without one, a throw anywhere in a page's
 * render leaves React with nothing to show and the reader with a white
 * screen; this turns that into the same dead-end block the pages use for a
 * failed fetch, plus a reload.
 *
 * The detail (message and stack) is deliberately dev-only: in production it
 * would be noise at best and internals at worst.
 */
export function RouteErrorBoundary() {
  const error = useRouteError();
  const detail = errorDetail(error);

  return (
    <div className="route-error">
      <ErrorState
        message={readerMessage(error)}
        onRetry={() => window.location.reload()}
      />
      {import.meta.env.DEV && detail ? (
        <pre className="route-error__detail">{detail}</pre>
      ) : null}
    </div>
  );
}
