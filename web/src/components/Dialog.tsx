import { useEffect, useId, useRef } from "react";
import type { MouseEvent, ReactNode } from "react";
import "./dialog.css";

type DialogProps = {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  className?: string;
};

/**
 * Accessible modal built on the native `<dialog>` element — `showModal()`
 * gives us focus trapping, a true top-layer stack, and `::backdrop` for
 * free from the browser. This wrapper adds the three things the native
 * element doesn't do on its own:
 *
 * - Focus returns to whatever triggered the dialog once it closes (the
 *   browser only puts focus *inside* the dialog on open).
 * - A true backdrop click (not a click on the panel) closes it, alongside
 *   the native Escape-to-close (which fires `cancel` then `close`).
 * - `aria-labelledby` is wired to the title automatically via `useId`.
 */
export function Dialog({ open, onClose, title, children, className }: DialogProps) {
  const ref = useRef<HTMLDialogElement | null>(null);
  const titleId = useId();
  const triggerRef = useRef<Element | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      triggerRef.current = document.activeElement;
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // Fires for Escape (native `cancel` -> `close`), our own close() calls
    // above, and the backdrop-click handler below — one place to notify
    // the caller and restore focus, regardless of how the dialog closed.
    function handleClose() {
      onClose();
      const trigger = triggerRef.current;
      if (trigger instanceof HTMLElement) trigger.focus();
    }
    dialog.addEventListener("close", handleClose);
    return () => dialog.removeEventListener("close", handleClose);
  }, [onClose]);

  // Clicking the backdrop dispatches a click to the <dialog> element
  // itself (there's no other node to target out there); clicking the
  // panel or anything inside it targets that descendant instead. So a
  // plain identity check on `target` reliably distinguishes the two.
  function handleBackdropClick(event: MouseEvent<HTMLDialogElement>) {
    if (event.target === ref.current) {
      ref.current?.close();
    }
  }

  const classes = ["dialog"];
  if (className) classes.push(className);

  return (
    <dialog
      ref={ref}
      className={classes.join(" ")}
      aria-labelledby={titleId}
      onClick={handleBackdropClick}
    >
      <div className="dialog__panel">
        <div className="dialog__head">
          <h2 id={titleId} className="dialog__title">
            {title}
          </h2>
          <button
            type="button"
            className="dialog__close"
            aria-label="Close"
            onClick={() => ref.current?.close()}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true" width="18" height="18">
              <path
                d="M5 5l14 14M19 5L5 19"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>
        <div className="dialog__body">{children}</div>
      </div>
    </dialog>
  );
}
