import { useId, useState } from "react";
import type { KeyboardEvent } from "react";

type VocabMultiSelectProps = {
  label: string;
  values: string[];
  options: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
};

/**
 * Type-ahead over known vocab values plus free-text create (Enter adds).
 * Selected values render as removable terracotta-outline chips.
 */
export function VocabMultiSelect({
  label,
  values,
  options,
  onChange,
  placeholder,
}: VocabMultiSelectProps) {
  const inputId = useId();
  const listId = useId();
  const optionIdBase = useId();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);

  const trimmed = query.trim();
  const lowered = trimmed.toLowerCase();
  const selected = new Set(values.map((value) => value.toLowerCase()));
  const suggestions = options
    .filter((option) => !selected.has(option.toLowerCase()))
    .filter((option) => !lowered || option.toLowerCase().includes(lowered))
    .slice(0, 8);
  const isKnown = options.some((option) => option.toLowerCase() === lowered);
  const showCreate = trimmed.length > 0 && !isKnown;
  const menuVisible = open && (suggestions.length > 0 || showCreate);

  // Flattened list of selectable menu entries (suggestions, then the
  // free-text "Add …" entry if present) so arrow keys can move a single
  // active index across both.
  const menuEntries: Array<{ id: string; value: string }> = menuVisible
    ? [
        ...suggestions.map((option, index) => ({
          id: `${optionIdBase}-${index}`,
          value: option,
        })),
        ...(showCreate ? [{ id: `${optionIdBase}-create`, value: trimmed }] : []),
      ]
    : [];
  const clampedActiveIndex =
    activeIndex >= 0 && activeIndex < menuEntries.length ? activeIndex : -1;
  const activeDescendant =
    clampedActiveIndex >= 0 ? menuEntries[clampedActiveIndex].id : undefined;

  function add(value: string) {
    const next = value.trim();
    if (!next) return;
    if (!selected.has(next.toLowerCase())) {
      onChange([...values, next]);
    }
    setQuery("");
    setActiveIndex(-1);
  }

  function remove(value: string) {
    onChange(values.filter((existing) => existing !== value));
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      if (!menuEntries.length) return;
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => (index + 1) % menuEntries.length);
    } else if (event.key === "ArrowUp") {
      if (!menuEntries.length) return;
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => (index <= 0 ? menuEntries.length - 1 : index - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (clampedActiveIndex >= 0) {
        add(menuEntries[clampedActiveIndex].value);
      } else if (trimmed) {
        add(trimmed);
      }
    } else if (event.key === "Escape") {
      setOpen(false);
      setActiveIndex(-1);
    } else if (event.key === "Backspace" && !query && values.length > 0) {
      remove(values[values.length - 1]);
    }
  }

  return (
    <div className="field">
      <label className="field__label" htmlFor={inputId}>
        {label}
      </label>
      <div className="vms">
        <input
          id={inputId}
          className="input"
          type="text"
          role="combobox"
          aria-expanded={menuVisible}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeDescendant}
          autoComplete="off"
          placeholder={placeholder}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
            setActiveIndex(-1);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onKeyDown={onKeyDown}
        />
        {menuVisible ? (
          <div className="vms__menu" id={listId} role="listbox">
            {suggestions.map((option, index) => (
              <button
                key={option}
                id={`${optionIdBase}-${index}`}
                type="button"
                role="option"
                aria-selected={index === clampedActiveIndex}
                className={
                  index === clampedActiveIndex
                    ? "vms__option is-active"
                    : "vms__option"
                }
                onMouseDown={(event) => {
                  event.preventDefault();
                  add(option);
                }}
              >
                {option}
              </button>
            ))}
            {showCreate ? (
              <button
                id={`${optionIdBase}-create`}
                type="button"
                role="option"
                aria-selected={suggestions.length === clampedActiveIndex}
                className={
                  suggestions.length === clampedActiveIndex
                    ? "vms__option vms__option--create is-active"
                    : "vms__option vms__option--create"
                }
                onMouseDown={(event) => {
                  event.preventDefault();
                  add(trimmed);
                }}
              >
                Add “{trimmed}”
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
      {values.length > 0 ? (
        <ul className="vms__chips" aria-label={`Selected ${label.toLowerCase()}`}>
          {values.map((value) => (
            <li key={value} className="chip">
              <span className="chip__text">{value}</span>
              <button
                type="button"
                className="chip__remove"
                aria-label={`Remove ${value}`}
                onClick={() => remove(value)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
