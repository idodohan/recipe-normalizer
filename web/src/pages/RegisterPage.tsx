import { useState } from "react";
import type { FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { Field, Input } from "../components/Field";
import { useUser, useUserActions } from "../hooks/useUser";
import "./auth.css";

export function RegisterPage() {
  const navigate = useNavigate();
  const { user, isLoading } = useUser();
  const { setUser } = useUserActions();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (!isLoading && user) {
    return <Navigate to="/" replace />;
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const { data: user, error: registerError } = await api.POST(
        "/api/auth/register",
        { body: { display_name: displayName, email, password } },
      );
      if (!user) {
        setError(
          apiErrorMessage(registerError, "Could not create your account."),
        );
        return;
      }
      setUser(user);
      navigate("/", { replace: true });
    } catch {
      setError("Could not reach the server. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth">
      <section className="auth__panel" aria-labelledby="register-title">
        <span className="auth__brand">Recipe Normalizer</span>
        <h1 className="auth__title" id="register-title">
          Start your cookbook
        </h1>
        <p className="auth__lede">
          Keep every recipe in one place, scaled to any table.
        </p>
        <form className="auth__form" onSubmit={(e) => void onSubmit(e)}>
          {error ? (
            <p className="auth__error" role="alert">
              {error}
            </p>
          ) : null}
          <Field label="Display name">
            {(props) => (
              <Input
                {...props}
                autoComplete="name"
                required
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            )}
          </Field>
          <Field label="Email">
            {(props) => (
              <Input
                {...props}
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            )}
          </Field>
          <Field label="Password">
            {(props) => (
              <Input
                {...props}
                type="password"
                autoComplete="new-password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>
          <Button
            type="submit"
            block
            disabled={submitting}
            className="auth__submit"
          >
            {submitting ? "Creating account…" : "Create account"}
          </Button>
        </form>
        <p className="auth__alt">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </section>
      <p className="auth__footer">Every recipe, in its proper measure.</p>
    </div>
  );
}
