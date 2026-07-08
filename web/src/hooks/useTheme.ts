import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "rn-theme";

type Theme = "light" | "dark" | null;

function readStoredTheme(): Theme {
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return raw === "light" || raw === "dark" ? raw : null;
}

function systemPrefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme) {
    root.setAttribute("data-theme", theme);
  } else {
    root.removeAttribute("data-theme");
  }
}

/**
 * Theme state: "light" | "dark" | null. `null` means "follow system" and is
 * the default until the user makes an explicit choice — after that, toggle()
 * sticks to the binary light/dark the user picked (no light -> dark ->
 * system cycle; see task brief, YAGNI).
 *
 * Persists to localStorage (`rn-theme`) and stamps `data-theme` on <html>.
 * A tiny inline script in index.html applies the stored value before first
 * paint so there's no flash of the wrong theme; this hook keeps it in sync
 * afterwards and reacts to live OS theme changes while `theme` is null.
 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme());
  const [systemDark, setSystemDark] = useState<boolean>(() =>
    systemPrefersDark(),
  );

  useEffect(() => {
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) =>
      setSystemDark(event.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    applyTheme(theme);
    if (theme) {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  }, [theme]);

  /** The theme actually in effect right now — resolves "system" (null). */
  const resolvedTheme: "light" | "dark" = theme ?? (systemDark ? "dark" : "light");

  const toggle = useCallback(() => {
    setTheme((current) => {
      const effective = current ?? (systemPrefersDark() ? "dark" : "light");
      return effective === "dark" ? "light" : "dark";
    });
  }, []);

  return { theme, resolvedTheme, toggle };
}
