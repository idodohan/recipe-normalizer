import { useSyncExternalStore } from "react";

export type ToastVariant = "success" | "error";

export type ToastOptions = {
  title: string;
  description?: string;
  variant?: ToastVariant;
};

export type ToastItem = ToastOptions & { id: number };

/**
 * Module-level toast store. Kept outside React so any code (mutation
 * `onSuccess`, non-component helpers, etc.) can call `toast(...)` without a
 * hook, and so subscribers re-render only when the list actually changes —
 * no context provider, no re-render storm.
 */
const MAX_VISIBLE = 3;

let toasts: ToastItem[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function notify() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): ToastItem[] {
  return toasts;
}

/** Show a toast. Safe to call from anywhere — not just React components. */
export function toast(options: ToastOptions): number {
  const id = nextId++;
  // Cap at MAX_VISIBLE — the oldest toast drops off the stack.
  toasts = [...toasts, { ...options, id }].slice(-MAX_VISIBLE);
  notify();
  return id;
}

/** Remove a toast by id (auto-dismiss timers and the manual dismiss button both call this). */
export function dismissToast(id: number): void {
  toasts = toasts.filter((t) => t.id !== id);
  notify();
}

/** Thin hook wrapper: subscribes the Toaster to the module-level store. */
export function useToastStore(): ToastItem[] {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** Convenience hook so call sites can `const { toast } = useToast()`. */
export function useToast(): { toast: typeof toast } {
  return { toast };
}
