import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { Button } from "../components/Button";
import { useUserActions, type User } from "../hooks/useUser";
import "./AppShell.css";

const NAV_ITEMS: Array<
  { label: string; to: string; soon?: false } | { label: string; soon: true }
> = [
  { label: "Cookbook", to: "/" },
  { label: "Inbox", to: "/inbox" },
  { label: "Catalog", to: "/catalog" },
  { label: "Shares", soon: true },
];

export function AppShell({ user }: { user: User }) {
  const navigate = useNavigate();
  const { clear } = useUserActions();

  async function signOut() {
    await api.POST("/api/auth/logout");
    clear();
    navigate("/login", { replace: true });
  }

  return (
    <div className="shell">
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
            <Button variant="ghost" onClick={() => void signOut()}>
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main className="shell__main">
        <Outlet />
      </main>
    </div>
  );
}
