import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import type { ChatMessageItem } from "./types";
import { ThinkingIndicator } from "./ThinkingIndicator";

type MessageListProps = {
  messages: ChatMessageItem[];
  /** Shown when there are no messages yet (before the first turn). */
  emptyHint?: string;
  /** Renders a "Try again" affordance on a `failed` message. */
  onRetry?: (id: string) => void;
};

/**
 * The chat transcript. `role="log"` + `aria-live="polite"` means assistive
 * tech announces each new message as it arrives without interrupting
 * whatever the user is doing (e.g. typing the next question) — the
 * accessible-name pattern for chat/log UIs. Auto-scrolls to the newest
 * message on every change; editorial styling (hairline-framed bubbles, no
 * chat-app gradients) lives in `ai-chat.css`.
 */
export function MessageList({ messages, emptyHint, onRetry }: MessageListProps) {
  const listRef = useRef<HTMLDivElement | null>(null);

  // Scroll the transcript itself, not via `scrollIntoView` on a bottom
  // sentinel: that scrolls EVERY scrollable ancestor including the document,
  // so each new message yanked the whole page down to the panel.
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  return (
    <div
      className="ai-msgs"
      ref={listRef}
      role="log"
      aria-live="polite"
      aria-relevant="additions text"
    >
      {messages.length === 0 && emptyHint ? <p className="ai-msgs__empty">{emptyHint}</p> : null}
      <ul className="ai-msgs__list">
        {messages.map((message) => (
          <li
            key={message.id}
            className={[
              "ai-msg",
              `ai-msg--${message.role}`,
              message.failed ? "ai-msg--failed" : "",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            {message.role === "assistant" ? (
              <span className="ai-msg__label">Assistant</span>
            ) : null}
            <div className="ai-msg__bubble">
              {message.pending ? (
                <ThinkingIndicator />
              ) : (
                <p className="ai-msg__content">{message.content}</p>
              )}
            </div>
            {message.sources && message.sources.length > 0 ? (
              <div className="ai-msg__sources">
                <span className="ai-msg__sources-label">Recipes referenced</span>
                <ul className="ai-msg__sources-list">
                  {message.sources.map((source) => (
                    <li key={source.id}>
                      <Link to={`/recipes/${source.id}`} className="chip chip--link">
                        {source.title ?? "…"}
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {message.failed ? (
              <p className="ai-msg__failed-note">
                Not sent.
                {onRetry ? (
                  <button
                    type="button"
                    className="ai-msg__retry"
                    onClick={() => onRetry(message.id)}
                  >
                    Try again
                  </button>
                ) : null}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
