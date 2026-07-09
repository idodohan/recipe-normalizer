import { useEffect, useRef } from "react";
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
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  return (
    <div className="ai-msgs" role="log" aria-live="polite" aria-relevant="additions text">
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
      <div ref={bottomRef} aria-hidden="true" />
    </div>
  );
}
