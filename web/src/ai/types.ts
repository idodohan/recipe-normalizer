/** Shared chat message shape for the ai/ChatPanel components.
 *
 * Deliberately NOT the generated `MessageOut` type — this also has to
 * represent client-only states (`pending` for the "thinking" placeholder,
 * `failed` for an optimistic user bubble whose send failed) that the API
 * never produces, so callers (RecipeChatPanel now; the Task-5 cookbook Q&A
 * panel later) map their fetched messages into this shape at the boundary.
 */
export type ChatRole = "user" | "assistant";

/** A recipe an assistant message drew on — `title` is null until it loads. */
export type ChatSource = {
  id: string;
  title: string | null;
};

export type ChatMessageItem = {
  /** Server message id, or a client-generated id for optimistic/placeholder rows. */
  id: string;
  role: ChatRole;
  /** Ignored while `pending` is true (the thinking indicator renders instead). */
  content: string;
  /** True for the trailing "assistant is thinking" placeholder row. */
  pending?: boolean;
  /** True for an optimistic user bubble whose send failed — offers a retry. */
  failed?: boolean;
  /**
   * Recipes THIS assistant message was grounded in (cookbook Q&A) — rendered
   * as chips under its own bubble, so a source is never misattributed to a
   * different turn.
   */
  sources?: ChatSource[];
};
