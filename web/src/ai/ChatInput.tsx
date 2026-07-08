import { useId, useRef } from "react";
import type { FormEvent, KeyboardEvent } from "react";

type ChatInputProps = {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  label: string;
  placeholder?: string;
  /** A reply is in flight — blocks a second send but keeps the field typable/focusable. */
  sending?: boolean;
  /** Hard-disables the whole control (e.g. conversation at its message cap). */
  disabled?: boolean;
};

/**
 * Label + textarea + send button. Enter sends, Shift+Enter inserts a
 * newline (the editor-textarea convention, not a plain `<input>`, so a
 * multi-line question is possible). The textarea itself is never disabled
 * by `sending` alone — only the send button is — so focus/typing is
 * uninterrupted while a reply is pending and naturally stays in the field
 * after a send (nothing blurs it).
 */
export function ChatInput({
  value,
  onChange,
  onSend,
  label,
  placeholder,
  sending = false,
  disabled = false,
}: ChatInputProps) {
  const id = useId();
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const canSend = !disabled && !sending && value.trim().length > 0;

  function submit() {
    if (!canSend) return;
    onSend();
    // Clicking the send button moves focus to the button by default; typing
    // Enter keeps it in the textarea already. Force it back into the
    // textarea either way so the next question can be typed immediately.
    textareaRef.current?.focus();
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    submit();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <form className="ai-input" onSubmit={handleSubmit}>
      <label htmlFor={id} className="ai-input__label">
        {label}
      </label>
      <div className="ai-input__row">
        <textarea
          id={id}
          ref={textareaRef}
          className="ai-input__field"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          rows={1}
          disabled={disabled}
        />
        <button
          type="submit"
          className="ai-input__send"
          disabled={!canSend}
          aria-label="Send message"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true" width="18" height="18">
            <path
              d="M4 12h15M13 6l6 6-6 6"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.75"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </div>
    </form>
  );
}
