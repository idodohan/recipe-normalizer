import { Navigate } from "react-router-dom";
import { useUser } from "../hooks/useUser";
import { AppShell } from "./AppShell";

/** Route guard: resolves the session, then renders the shell or bounces to /login. */
export function AuthenticatedApp() {
  const { user, isLoading } = useUser();
  if (isLoading) {
    // Quiet pre-shell state; resolves in a single round-trip.
    return null;
  }
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  return <AppShell user={user} />;
}
