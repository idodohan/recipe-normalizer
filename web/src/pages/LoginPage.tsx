import { useState } from "react";
import type { FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { Field, Input } from "../components/Field";
import { useUser, useUserActions } from "../hooks/useUser";
import "./auth.css";

export function LoginPage() {
  const navigate = useNavigate();
  const { user, isLoading } = useUser();
  const { setUser } = useUserActions();
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
      const { data, error: apiError } = await api.POST("/api/auth/login", {
        body: { email, password },
      });
      if (data) {
        setUser(data);
        navigate("/", { replace: true });
        return;
      }
      setError(apiErrorMessage(apiError, "Could not sign in. Please try again."));
    } catch {
      setError("Could not reach the server. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth">
      <section className="auth__panel" aria-labelledby="login-title">
        <span className="auth__brand">Recipe Normalizer</span>
        <h1 className="auth__title" id="login-title">
          Welcome back
        </h1>
        <p className="auth__lede">Sign in to open your cookbook.</p>
        <form className="auth__form" onSubmit={(e) => void onSubmit(e)}>
          {error ? (
            <p className="auth__error" role="alert">
              {error}
            </p>
          ) : null}
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
                autoComplete="current-password"
                required
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
            {submitting ? "Signing in…" : "Sign in"}
          </Button>
        </form>
        <p className="auth__alt">
          New here? <Link to="/register">Create an account</Link>
        </p>
      </section>
      <p className="auth__footer">Every recipe, in its proper measure.</p>
    </div>
  );
}
