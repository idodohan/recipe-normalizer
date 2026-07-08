import { useId, useRef } from "react";
import type { KeyboardEvent, RefCallback } from "react";

/**
 * Headless roving-tabindex helper for `tablist` / `tab` / `tabpanel` triples.
 *
 * Callers keep their own markup and CSS (this doesn't render anything) —
 * spread `getTabProps(index)` onto each tab button and `getPanelProps(index)`
 * onto the corresponding panel. Handles ArrowLeft/ArrowRight/Home/End,
 * `aria-selected`, `aria-controls`/`id` wiring, and moving DOM focus to the
 * newly-active tab (roving tabindex: only the selected tab is in the
 * sequential tab order; the rest are reachable via arrow keys).
 */
export function useTabList({
  count,
  activeIndex,
  onChange,
  idBase,
}: {
  count: number;
  activeIndex: number;
  onChange: (index: number) => void;
  idBase?: string;
}) {
  const autoId = useId();
  const base = idBase ?? autoId;
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  function focusTab(index: number) {
    tabRefs.current[index]?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (count === 0) return;
    let next: number | null = null;
    if (event.key === "ArrowRight") next = (activeIndex + 1) % count;
    else if (event.key === "ArrowLeft") next = (activeIndex - 1 + count) % count;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = count - 1;
    if (next !== null) {
      event.preventDefault();
      onChange(next);
      focusTab(next);
    }
  }

  return {
    tablistProps: { role: "tablist" as const },
    getTabProps(index: number) {
      const selected = index === activeIndex;
      const ref: RefCallback<HTMLButtonElement> = (el) => {
        tabRefs.current[index] = el;
      };
      return {
        id: `${base}-tab-${index}`,
        role: "tab" as const,
        "aria-selected": selected,
        "aria-controls": `${base}-panel-${index}`,
        tabIndex: selected ? 0 : -1,
        ref,
        onKeyDown,
        onClick: () => onChange(index),
      };
    },
    getPanelProps(index: number) {
      return {
        id: `${base}-panel-${index}`,
        role: "tabpanel" as const,
        "aria-labelledby": `${base}-tab-${index}`,
      };
    },
  };
}
