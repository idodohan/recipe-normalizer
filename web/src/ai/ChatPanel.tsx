import type { ReactNode } from "react";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import type { ChatMessageItem } from "./types";
import "./ai-chat.css";

type ChatPanelProps = {
  messages: ChatMessageItem[];
  draft: string;
  onDraftChange: (value: string) => void;
  onSend: () => void;
  onRetry?: (id: string) => void;
  sending?: boolean;
  disabled?: boolean;
  inputLabel: string;
  inputPlaceholder?: string;
  emptyHint?: string;
  /** Inline status/error line above the input (e.g. "conversation full"). */
  statusMessage?: string | null;
  /** Rendered above the transcript — e.g. cookbook Q&A's "sources" chips row (Task 5). */
  children?: ReactNode;
};

/**
 * Editorial chat surface shared by per-recipe chat (RecipeChatPanel) and,
 * later, cookbook Q&A (Task 5) — everything data-fetching/mutation-shaped
 * lives in the feature-specific wrapper; this component is pure
 * presentation over a message list + input.
 */
export function ChatPanel({
  messages,
  draft,
  onDraftChange,
  onSend,
  onRetry,
  sending = false,
  disabled = false,
  inputLabel,
  inputPlaceholder,
  emptyHint,
  statusMessage,
  children,
}: ChatPanelProps) {
  return (
    <div className="ai-panel">
      {children}
      <MessageList messages={messages} emptyHint={emptyHint} onRetry={onRetry} />
      {statusMessage ? (
        <p className="ai-panel__status" role="alert">
          {statusMessage}
        </p>
      ) : null}
      <ChatInput
        value={draft}
        onChange={onDraftChange}
        onSend={onSend}
        sending={sending}
        disabled={disabled}
        label={inputLabel}
        placeholder={inputPlaceholder}
      />
    </div>
  );
}
