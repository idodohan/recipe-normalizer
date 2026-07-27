import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { apiErrorEnvelope, apiErrorMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";
import { ChatPanel } from "../../ai/ChatPanel";
import type { ChatMessageItem } from "../../ai/types";

type RecipeChatPanelProps = {
  recipeId: string;
  recipeTitle: string;
};

/**
 * "Ask about this recipe" — a disclosure section (matching CollectionsControl's
 * toggle pattern) that lazily loads or creates the user's `recipe_chat`
 * conversation for this recipe and wires it to the shared `ChatPanel`.
 *
 * Data flow:
 *  - On open, list this user's conversations for `recipe_id` (owner-scoped —
 *    the backend never returns another user's conversation about the same
 *    recipe) and adopt the most-recently-active one, if any.
 *  - The conversation is created lazily, on the FIRST send, not on open —
 *    opening the panel with nothing asked yet costs nothing server-side.
 *  - Sending is optimistic: a local user bubble + a "thinking" placeholder
 *    appear immediately; on success both are replaced by the server's
 *    messages (refetched); on failure the bubble is marked `failed` (with a
 *    retry) and a toast fires. A `not_found` failure means access to the
 *    recipe was lost since the conversation started (e.g. removed from a
 *    shared cookbook) — that's treated as "truly unavailable" and the panel
 *    stops accepting new input rather than retrying against a recipe the
 *    user can no longer read.
 */
export function RecipeChatPanel({ recipeId, recipeTitle }: RecipeChatPanelProps) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  // `conversationId` state only updates on the next render, which isn't fast
  // enough for `mutationFn`: it needs to see a conversation created earlier
  // in the SAME attempt (or by a still-in-flight prior attempt) before a
  // retry decides whether to create another one. This ref is the
  // synchronously-readable source of truth mutationFn consults; state stays
  // for rendering and is kept in sync alongside it.
  const conversationIdRef = useRef<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingUser, setPendingUser] = useState<ChatMessageItem | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [capped, setCapped] = useState(false);
  const panelId = useId();

  const list = useQuery({
    queryKey: ["ai", "recipe-chat", recipeId],
    enabled: open,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/ai/conversations", {
        params: { query: { recipe_id: recipeId } },
      });
      if (error) throw error;
      return data;
    },
  });

  // Adopt the most-recently-active conversation once the list loads. Every
  // conversation returned here has this `recipe_id`, so it's necessarily
  // `kind: "recipe_chat"` (only cookbook_qa conversations have a null
  // recipe_id) — no client-side kind filter needed.
  useEffect(() => {
    if (conversationId === null && list.data && list.data.length > 0) {
      const id = list.data[0].id;
      conversationIdRef.current = id;
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setConversationId(id);
    }
  }, [list.data, conversationId]);

  // The initial list fetch tells us whether a conversation already exists
  // server-side. Until it settles, `conversationId`/`conversationIdRef` may
  // still be null even though one exists — sending before then would create
  // a duplicate. `list` is enabled only while `open`, so this is only ever
  // consulted while the panel is actually rendered.
  const listSettled = list.isSuccess || list.isError;

  const detail = useQuery({
    queryKey: ["ai", "conversation", conversationId],
    enabled: conversationId !== null,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/ai/conversations/{conversation_id}", {
        params: { path: { conversation_id: conversationId! } },
      });
      if (error) throw error;
      return data;
    },
  });

  const send = useMutation({
    mutationFn: async (content: string) => {
      let convId = conversationIdRef.current;
      if (convId === null) {
        const { data, error } = await api.POST("/api/ai/conversations", {
          body: { recipe_id: recipeId, kind: "recipe_chat" },
        });
        if (error) throw error;
        convId = data.id;
        // Record the created conversation the instant it exists — BEFORE
        // the message POST that can still fail (e.g. the ai_chat rate
        // limit sits on the messages endpoint). A ref is used because it's
        // readable synchronously; `conversationId` state wouldn't be
        // visible to a retry fired within the same render cycle. Without
        // this, a message-POST failure leaves conversationIdRef null and
        // "Try again" would create a second, orphaned conversation.
        conversationIdRef.current = convId;
        setConversationId(convId);
      }
      const { error } = await api.POST("/api/ai/conversations/{conversation_id}/messages", {
        params: { path: { conversation_id: convId } },
        body: { content },
      });
      if (error) throw error;
      return { conversationId: convId };
    },
    onSuccess: async ({ conversationId: convId }) => {
      // Reconciles state with the ref (already set above in the common
      // case) — kept so onSuccess remains the single place that guarantees
      // consistency regardless of how the id was resolved.
      conversationIdRef.current = convId;
      setConversationId(convId);
      // Wait for the transcript to refetch BEFORE dropping the optimistic
      // bubble/thinking placeholder, so the real messages are already in
      // the query cache when they replace it — no flash of an empty list.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["ai", "conversation", convId] }),
        queryClient.invalidateQueries({ queryKey: ["ai", "recipe-chat", recipeId] }),
      ]);
      setPendingUser(null);
    },
    onError: (error, content) => {
      const { code } = apiErrorEnvelope(error);
      if (code === "not_found") {
        setUnavailable(true);
        setPendingUser(null);
        toast({
          title: "This recipe is no longer available",
          description: "You may have lost access to it.",
          variant: "error",
        });
        return;
      }
      if (code === "conversation_full") {
        setCapped(true);
        setPendingUser(null);
        toast({
          title: "Conversation limit reached",
          description: apiErrorMessage(error, "Start a fresh question later."),
          variant: "error",
        });
        return;
      }
      setPendingUser((prev) => (prev ? { ...prev, failed: true } : prev));
      // Mirror the unsent text back into the draft. The failed bubble lives
      // only in `pendingUser`, which dies with the panel — without this,
      // collapsing the disclosure (or navigating away) silently destroys what
      // the user wrote. Never clobbers a newer draft they've started typing.
      setDraft((current) => (current.trim().length > 0 ? current : content));
      toast({
        title: "Could not send message",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  function handleSend() {
    const trimmed = draft.trim();
    if (!trimmed || send.isPending || !listSettled) return;
    setDraft("");
    setPendingUser({ id: `optimistic-${crypto.randomUUID()}`, role: "user", content: trimmed });
    send.mutate(trimmed);
  }

  function handleRetry(id: string) {
    // `send.isPending` guard: without it a double-click on "Try again" fires
    // two turns for the same question (and, on a first send, could create two
    // conversations).
    if (pendingUser?.id !== id || send.isPending) return;
    const content = pendingUser.content;
    // The retry takes ownership of the text again, so drop the copy onError
    // parked in the draft — unless the user has since edited it into
    // something else.
    setDraft((current) => (current === content ? "" : current));
    setPendingUser({ ...pendingUser, failed: false });
    send.mutate(content);
  }

  const baseMessages: ChatMessageItem[] = (detail.data?.messages ?? []).map((message) => ({
    id: message.id,
    role: message.role,
    content: message.content,
  }));

  const messages: ChatMessageItem[] = [...baseMessages];
  if (pendingUser) {
    messages.push(pendingUser);
    if (send.isPending && !pendingUser.failed) {
      messages.push({ id: "thinking", role: "assistant", content: "", pending: true });
    }
  }

  const statusMessage = unavailable
    ? "This recipe is no longer available to you."
    : capped
      ? "This conversation has reached its message limit."
      : list.isError
        ? apiErrorMessage(list.error, "Could not load this conversation.")
        : !listSettled
          ? "Loading conversation…"
          : null;

  return (
    <section className="rd__chat">
      <button
        type="button"
        className="rd__chat-toggle"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        Ask about this recipe
      </button>

      {open ? (
        <div id={panelId} className="rd__chat-panel">
          <ChatPanel
            messages={messages}
            draft={draft}
            onDraftChange={setDraft}
            onSend={handleSend}
            onRetry={handleRetry}
            sending={send.isPending}
            disabled={unavailable || capped || !listSettled}
            inputLabel={`Ask a question about ${recipeTitle}`}
            inputPlaceholder="e.g. Can I substitute butter for oil?"
            emptyHint="Ask anything about this recipe — ingredients, steps, timing. Answers are grounded in the recipe only."
            statusMessage={statusMessage}
          />
        </div>
      ) : null}
    </section>
  );
}
