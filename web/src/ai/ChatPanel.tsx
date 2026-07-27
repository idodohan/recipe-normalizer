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
};

/**
 * Editorial chat surface shared by per-recipe chat (RecipeChatPanel) and
 * cookbook Q&A (CookbookQaPanel) — everything data-fetching/mutation-shaped
 * lives in the feature-specific wrapper; this component is pure
 * presentation over a message list + input. Per-answer extras (cookbook
 * Q&A's referenced-recipe chips) ride on the messages themselves
 * (`ChatMessageItem.sources`) rather than a panel-level slot, so they render
 * with the turn they belong to.
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
}: ChatPanelProps) {
  return (
    <div className="ai-panel">
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
