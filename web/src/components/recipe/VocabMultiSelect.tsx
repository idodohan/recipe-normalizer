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
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);

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

  function add(value: string) {
    const next = value.trim();
    if (!next) return;
    if (!selected.has(next.toLowerCase())) {
      onChange([...values, next]);
    }
    setQuery("");
  }

  function remove(value: string) {
    onChange(values.filter((existing) => existing !== value));
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      if (trimmed) add(trimmed);
    } else if (event.key === "Escape") {
      setOpen(false);
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
          autoComplete="off"
          placeholder={placeholder}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onKeyDown={onKeyDown}
        />
        {menuVisible ? (
          <div className="vms__menu" id={listId} role="listbox">
            {suggestions.map((option) => (
              <button
                key={option}
                type="button"
                role="option"
                aria-selected={false}
                className="vms__option"
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
                type="button"
                role="option"
                aria-selected={false}
                className="vms__option vms__option--create"
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
