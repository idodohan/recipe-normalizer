import { useEffect, useRef, useState } from "react";
import { dismissToast, useToastStore } from "../hooks/useToast";
import type { ToastItem } from "../hooks/useToast";
import "./toaster.css";

const AUTO_DISMISS_MS = 4000;
const EXIT_MS = 200;

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

function ToastCard({ item }: { item: ToastItem }) {
  const [entered, setEntered] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const remove = () => {
    if (prefersReducedMotion()) {
      dismissToast(item.id);
      return;
    }
    setLeaving(true);
    setTimeout(() => dismissToast(item.id), EXIT_MS);
  };

  const startTimer = () => {
    timerRef.current = setTimeout(remove, AUTO_DISMISS_MS);
  };

  const clearTimer = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  useEffect(() => {
    // Enter transition: mount off-state, then flip a frame later.
    const raf = requestAnimationFrame(() => setEntered(true));
    startTimer();
    return () => {
      cancelAnimationFrame(raf);
      clearTimer();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const variant = item.variant ?? "success";

  return (
    <li
      className={[
        "toast",
        `toast--${variant}`,
        entered ? "toast--entered" : "",
        leaving ? "toast--leaving" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      onMouseEnter={clearTimer}
      onMouseLeave={() => {
        clearTimer();
        startTimer();
      }}
      onFocus={clearTimer}
      onBlur={() => {
        clearTimer();
        startTimer();
      }}
    >
      <div className="toast__body">
        <p className="toast__title">{item.title}</p>
        {item.description ? (
          <p className="toast__description">{item.description}</p>
        ) : null}
      </div>
      <button
        type="button"
        className="toast__dismiss"
        aria-label="Dismiss"
        onClick={remove}
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 12 12"
          aria-hidden="true"
          focusable="false"
        >
          <path
            d="M1 1L11 11M11 1L1 11"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
        </svg>
      </button>
    </li>
  );
}

/**
 * Toast stack — fixed bottom-right (bottom-center under 640px). Announces
 * politely via `role="status"`/`aria-live="polite"`; never moves focus.
 */
export function Toaster() {
  const items = useToastStore();

  return (
    <ol className="toast-stack" role="status" aria-live="polite">
      {items.map((item) => (
        <ToastCard key={item.id} item={item} />
      ))}
    </ol>
  );
}
