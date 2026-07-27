import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { apiErrorEnvelope, apiErrorMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";
import { ChatPanel } from "../../ai/ChatPanel";
import type { ChatMessageItem, ChatRole, ChatSource } from "../../ai/types";
import "../../ai/ai-chat.css";
import "./cookbook-qa.css";

/** One completed turn held locally — see the `localTurns` note in the component. */
type LocalTurn = {
  user: ChatMessageItem;
  assistant: ChatMessageItem;
};

function toChatItem(message: { id: string; role: ChatRole; content: string }): ChatMessageItem {
  return { id: message.id, role: message.role, content: message.content };
}

/**
 * "Ask your cookbook" — a disclosure section on CookbookPage (same toggle
 * pattern as `RecipeChatPanel`) wired to `POST /api/ai/cookbook-qa`: a
 * tool-use turn that searches the user's OWN cookbook rather than grounding
 * on one recipe.
 *
 * Data flow differs from `RecipeChatPanel` in two important ways:
 *
 *  1. Cookbook Q&A is a SINGLE atomic call — `ai.service.cookbook_qa_turn`
 *     creates the conversation server-side (only after the LLM call succeeds)
 *     in the same request that answers the question, so there's no separate
 *     create-conversation-then-post-message pair. The classic double-create
 *     race Task 3 guarded against can't happen the same way here.
 *     `conversationIdRef` is still the source of truth `mutationFn` reads (not
 *     `conversationId` state, which only updates on the next render) —
 *     mirroring the Task-3 pattern exactly — so a retry fired within the same
 *     interaction, or a second question typed before this render has
 *     committed, reuses whatever id was most recently adopted rather than
 *     risking a stale read.
 *  2. The response CONTAINS the assistant message, so a settled turn is
 *     appended to `localTurns` instead of triggering a full transcript
 *     refetch per question (see `onSuccess`).
 *
 * Conversation continuity: `GET /api/ai/conversations` only filters by
 * `recipe_id` (cookbook Q&A conversations have none — that's precisely what
 * distinguishes them from recipe-chat ones), so there is no server-side
 * `kind` filter to ask for. On open, this fetches the user's full
 * conversation list unfiltered (already sorted most-recently-active first)
 * and adopts the first entry with `kind === "cookbook_qa"` client-side —
 * continuing the latest cookbook-wide conversation rather than starting a
 * fresh one every session.
 */
export function CookbookQaPanel() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const conversationIdRef = useRef<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingUser, setPendingUser] = useState<ChatMessageItem | null>(null);
  const [capped, setCapped] = useState(false);
  // Turns already answered by `POST /api/ai/cookbook-qa` in this session. The
  // response carries the assistant message, so these render immediately with
  // no transcript refetch behind them; each pair drops out again as soon as
  // the canonical transcript catches up (see `transcript` below).
  const [localTurns, setLocalTurns] = useState<LocalTurn[]>([]);
  // Messages that outlived their conversation id — see the `not_found` branch.
  const [orphanedMessages, setOrphanedMessages] = useState<ChatMessageItem[]>([]);
  // Referenced-recipe ids keyed by the ASSISTANT MESSAGE they came back with,
  // so every answer's chips stay attached to that answer instead of one
  // "latest sources" row floating above the whole transcript. Keying by a real
  // message id is also what makes errors self-clearing: a failed turn produces
  // no message, so there's nothing to attribute and nothing to reset.
  const [sourcesByMessageId, setSourcesByMessageId] = useState<Record<string, string[]>>({});
  const panelId = useId();

  const list = useQuery({
    queryKey: ["ai", "cookbook-qa", "conversations"],
    enabled: open,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/ai/conversations");
      if (error) throw error;
      return data;
    },
  });

  // Adopt the most-recently-active `cookbook_qa` conversation once the list
  // loads — see the conversation-continuity note above for why this filters
  // client-side rather than passing a server-side `kind` query param.
  useEffect(() => {
    if (conversationId === null && list.data) {
      const latest = list.data.find((conversation) => conversation.kind === "cookbook_qa");
      if (latest) {
        conversationIdRef.current = latest.id;
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setConversationId(latest.id);
      }
    }
  }, [list.data, conversationId]);

  // Until the initial list fetch settles, a continuing conversation may not
  // have been adopted yet — sending before then would risk starting a
  // second cookbook_qa conversation instead of continuing the existing one.
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

  // Titles for the referenced-recipe chips: `cookbook-qa`'s response gives
  // ids only (see `CookbookQaOut.referenced_recipe_ids`), and there's no
  // single always-loaded `["recipes"]` cache to read from here — CookbookPage's
  // own list is cached per active filter set (`["recipes", filters]`), not a
  // stable key this component could reliably probe. Instead this fetches
  // each referenced id individually via the SAME `["recipe", id]` query key
  // `RecipeDetailPage` uses, so the two share a cache: a recipe visited (or
  // about to be visited via a chip click) before/after asking about it in
  // Q&A is fetched once, not twice. Ids are deduped across every answer, so
  // a recipe cited by three turns is still one fetch.
  const sourceRecipeIds = [...new Set(Object.values(sourcesByMessageId).flat())];

  const recipeQueries = useQueries({
    queries: sourceRecipeIds.map((recipeId) => ({
      queryKey: ["recipe", recipeId],
      queryFn: async () => {
        const { data, error } = await api.GET("/api/recipes/{recipe_id}", {
          params: { path: { recipe_id: recipeId } },
        });
        if (error) throw error;
        return data;
      },
      staleTime: 60_000,
    })),
  });

  const sourceTitles = new Map<string, string | null>();
  sourceRecipeIds.forEach((recipeId, index) => {
    const query = recipeQueries[index];
    // A recipe we can't load makes a dead chip — leave it out entirely rather
    // than render a link stuck on its loading placeholder.
    if (!query || query.isError) return;
    sourceTitles.set(recipeId, query.data?.title ?? null);
  });

  function withSources(message: ChatMessageItem): ChatMessageItem {
    const ids = sourcesByMessageId[message.id];
    if (!ids) return message;
    const sources: ChatSource[] = ids
      .filter((recipeId) => sourceTitles.has(recipeId))
      .map((recipeId) => ({ id: recipeId, title: sourceTitles.get(recipeId) ?? null }));
    return sources.length > 0 ? { ...message, sources } : message;
  }

  const fetchedMessages = (detail.data?.messages ?? []).map(toChatItem);
  const fetchedIds = new Set(fetchedMessages.map((message) => message.id));
  const transcript: ChatMessageItem[] = [
    ...orphanedMessages,
    ...fetchedMessages,
    // A local turn whose assistant message has since arrived in the fetched
    // transcript is already on screen — drop the whole pair, since its user
    // half carries a client-side id that wouldn't dedupe on its own.
    ...localTurns
      .filter((turn) => !fetchedIds.has(turn.assistant.id))
      .flatMap((turn) => [turn.user, turn.assistant]),
  ].map(withSources);

  const send = useMutation({
    mutationFn: async (content: string) => {
      const { data, error } = await api.POST("/api/ai/cookbook-qa", {
        body: { content, conversation_id: conversationIdRef.current },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data, content) => {
      conversationIdRef.current = data.conversation_id;
      setConversationId(data.conversation_id);
      if (data.referenced_recipe_ids.length > 0) {
        setSourcesByMessageId((previous) => ({
          ...previous,
          [data.message.id]: data.referenced_recipe_ids,
        }));
      }
      // The response already IS this turn — the answer plus the text we just
      // sent — so it goes straight into the transcript. Refetching the whole
      // conversation per question (the previous behaviour) spent a round trip
      // re-downloading everything the user had already read.
      setLocalTurns((previous) => [
        ...previous,
        {
          user: { id: `sent-${crypto.randomUUID()}`, role: "user", content },
          assistant: toChatItem(data.message),
        },
      ]);
      setPendingUser(null);
      // Still mark the cached transcript stale, but WITHOUT refetching it now
      // (`refetchType: "none"`): the next mount or window refocus picks up the
      // canonical copy, and the local turn dedupes itself away when it lands.
      void queryClient.invalidateQueries({
        queryKey: ["ai", "conversation", data.conversation_id],
        refetchType: "none",
      });
      // The list does need to be current — it drives which conversation is
      // adopted on the next open, and its ordering just changed.
      void queryClient.invalidateQueries({ queryKey: ["ai", "cookbook-qa", "conversations"] });
    },
    onError: (error, content) => {
      const { code } = apiErrorEnvelope(error);
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
      if (code === "not_found") {
        // The adopted conversation id no longer resolves (e.g. it existed in
        // another tab/session state that's gone stale) — drop it so the next
        // send starts a fresh conversation instead of retrying against an id
        // that will never succeed. Dropping it also disables the `detail`
        // query, which would otherwise take the visible transcript down at the
        // same instant the error toast appears; snapshot what's on screen first
        // so the history the user was reading survives. `localTurns` is
        // cleared because the snapshot already contains those turns.
        setOrphanedMessages(transcript);
        setLocalTurns([]);
        conversationIdRef.current = null;
        setConversationId(null);
      }
      setPendingUser((previous) => (previous ? { ...previous, failed: true } : previous));
      // Mirror the unsent text back into the draft: the failed bubble lives
      // only in `pendingUser`, which dies when the disclosure closes. Never
      // clobbers a newer draft the user has started typing.
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
    // the same question twice.
    if (pendingUser?.id !== id || send.isPending) return;
    const content = pendingUser.content;
    // The retry takes ownership of the text again, so drop the copy onError
    // parked in the draft — unless the user has since edited it.
    setDraft((current) => (current === content ? "" : current));
    setPendingUser({ ...pendingUser, failed: false });
    send.mutate(content);
  }

  const messages: ChatMessageItem[] = [...transcript];
  if (pendingUser) {
    messages.push(pendingUser);
    if (send.isPending && !pendingUser.failed) {
      messages.push({ id: "thinking", role: "assistant", content: "", pending: true });
    }
  }

  const statusMessage = capped
    ? "This conversation has reached its message limit."
    : list.isError
      ? apiErrorMessage(list.error, "Could not load your previous cookbook conversations.")
      : !listSettled
        ? "Loading conversation…"
        : null;

  return (
    <section className="cookbook-qa">
      <button
        type="button"
        className="cookbook-qa__toggle"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        Ask your cookbook
      </button>

      {open ? (
        <div id={panelId} className="cookbook-qa__panel">
          <ChatPanel
            messages={messages}
            draft={draft}
            onDraftChange={setDraft}
            onSend={handleSend}
            onRetry={handleRetry}
            sending={send.isPending}
            disabled={capped || !listSettled}
            inputLabel="Ask a question about your cookbook"
            inputPlaceholder="e.g. What vegetarian dinners do I have?"
            emptyHint="Ask anything about your saved recipes — “what can I make with chicken and rice?” or “which recipes are gluten-free?” Answers are grounded in your own cookbook only."
            statusMessage={statusMessage}
          />
        </div>
      ) : null}
    </section>
  );
}
