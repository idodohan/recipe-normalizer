import { useEffect, useRef } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { api } from "../api/client";
import { Button } from "../components/Button";
import { useTheme } from "../hooks/useTheme";
import { useUserActions, type User } from "../hooks/useUser";
import "./AppShell.css";

const NAV_ITEMS: Array<
  { label: string; to: string; soon?: false } | { label: string; soon: true }
> = [
  { label: "Cookbook", to: "/" },
  { label: "Inbox", to: "/inbox" },
  { label: "Catalog", to: "/catalog" },
  { label: "Shares", to: "/shares" },
];

export function AppShell({ user }: { user: User }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { clear } = useUserActions();
  const { resolvedTheme, toggle } = useTheme();
  const mainRef = useRef<HTMLElement>(null);
  // Tracks the previously-seen pathname so we can detect real navigations.
  // Initialized lazily via useRef — unlike a mutable "have I run yet" flag
  // flipped inside the effect body, this survives React StrictMode's
  // dev-only double-invoke of effects (mount -> cleanup -> mount) without
  // falsely treating the second invoke as a real navigation, since the
  // pathname doesn't change across that double-invoked mount.
  const prevPathRef = useRef(location.pathname);

  // Move focus to the main content on route changes, so screen-reader and
  // keyboard users land somewhere sensible instead of staying on the old
  // nav link. Skip the very first render — don't steal focus on initial load.
  // Compares against the *previous* pathname (not the one captured at mount)
  // so returning to an earlier route — including the first-visited one —
  // still refocuses #main.
  useEffect(() => {
    if (prevPathRef.current !== location.pathname) {
      mainRef.current?.focus();
    }
    prevPathRef.current = location.pathname;
  }, [location.pathname]);

  async function signOut() {
    await api.POST("/api/auth/logout");
    clear();
    navigate("/login", { replace: true });
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="shell__topbar">
        <div className="shell__topbar-inner">
          <Link className="shell__wordmark" to="/">
            Recipe Normalizer
          </Link>
          <nav className="shell__nav" aria-label="Primary">
            {NAV_ITEMS.map((item) =>
              item.soon ? (
                <span
                  key={item.label}
                  className="shell__nav-link shell__nav-link--disabled"
                  aria-disabled="true"
                >
                  {item.label}
                  <span className="shell__soon">soon</span>
                </span>
              ) : (
                <NavLink
                  key={item.label}
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) =>
                    isActive
                      ? "shell__nav-link shell__nav-link--active"
                      : "shell__nav-link"
                  }
                >
                  {item.label}
                </NavLink>
              ),
            )}
          </nav>
          <div className="shell__user">
            <span className="shell__user-name">
              <strong>{user.display_name}</strong>
            </span>
            <button
              type="button"
              className="shell__theme-toggle"
              onClick={toggle}
              aria-label={
                resolvedTheme === "dark"
                  ? "Switch to light theme"
                  : "Switch to dark theme"
              }
            >
              {resolvedTheme === "dark" ? (
                <svg
                  width="20"
                  height="20"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <circle cx="12" cy="12" r="4" />
                  <line x1="12" y1="2" x2="12" y2="4" />
                  <line x1="12" y1="20" x2="12" y2="22" />
                  <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
                  <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
                  <line x1="2" y1="12" x2="4" y2="12" />
                  <line x1="20" y1="12" x2="22" y2="12" />
                  <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
                  <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
                </svg>
              ) : (
                <svg
                  width="20"
                  height="20"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z" />
                </svg>
              )}
            </button>
            <Button variant="ghost" onClick={() => void signOut()}>
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main id="main" className="shell__main" tabIndex={-1} ref={mainRef}>
        <Outlet />
      </main>
    </div>
  );
}
