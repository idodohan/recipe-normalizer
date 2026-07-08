import { Navigate } from "react-router-dom";
import { useUser } from "../hooks/useUser";
import { AppShell } from "./AppShell";
import "./AppShell.css";

/** Route guard: resolves the session, then renders the shell or bounces to /login. */
export function AuthenticatedApp() {
  const { user, isLoading } = useUser();
  if (isLoading) {
    // Quiet pre-shell state; resolves in a single round-trip. A full-viewport
    // paper screen with the wordmark avoids the blank flash of `null`.
    return (
      <div className="auth-loading" role="status">
        <p className="auth-loading__wordmark" aria-hidden="true">
          Recipe Normalizer
        </p>
        <span className="visually-hidden">Signing you in…</span>
      </div>
    );
  }
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  return <AppShell user={user} />;
}
