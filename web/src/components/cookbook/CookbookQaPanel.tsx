import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { apiErrorEnvelope, apiErrorMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";
import { ChatPanel } from "../../ai/ChatPanel";
import type { ChatMessageItem } from "../../ai/types";
import "../../ai/ai-chat.css";
import "./cookbook-qa.css";

/**
 * "Ask your cookbook" — a disclosure section on CookbookPage (same toggle
 * pattern as `RecipeChatPanel`) wired to `POST /api/ai/cookbook-qa`: a
 * tool-use turn that searches the user's OWN cookbook rather than grounding
 * on one recipe.
 *
 * Data flow differs from `RecipeChatPanel` in one important way: cookbook
 * Q&A is a SINGLE atomic call — `ai.service.cookbook_qa_turn` creates the
 * conversation server-side (only after the LLM call succeeds) in the same
 * request that answers the question, so there's no separate
 * create-conversation-then-post-message pair. The classic double-create race
 * Task 3 guarded against can't happen the same way here. `conversationIdRef`
 * is still the source of truth `mutationFn` reads (not `conversationId`
 * state, which only updates on the next render) — mirroring the Task-3
 * pattern exactly — so a retry fired within the same interaction, or a
 * second question typed before this render has committed, reuses whatever
 * id was most recently adopted rather than risking a stale read.
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
  const [referencedRecipeIds, setReferencedRecipeIds] = useState<string[]>([]);
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

  const send = useMutation({
    mutationFn: async (content: string) => {
      const { data, error } = await api.POST("/api/ai/cookbook-qa", {
        body: { content, conversation_id: conversationIdRef.current },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: async (data) => {
      conversationIdRef.current = data.conversation_id;
      setConversationId(data.conversation_id);
      setReferencedRecipeIds(data.referenced_recipe_ids);
      // Wait for the transcript to refetch BEFORE dropping the optimistic
      // bubble/thinking placeholder, so the real messages are already in
      // the query cache when they replace it — no flash of an empty list.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["ai", "conversation", data.conversation_id] }),
        queryClient.invalidateQueries({ queryKey: ["ai", "cookbook-qa", "conversations"] }),
      ]);
      setPendingUser(null);
    },
    onError: (error) => {
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
        // that will never succeed.
        conversationIdRef.current = null;
        setConversationId(null);
      }
      setPendingUser((prev) => (prev ? { ...prev, failed: true } : prev));
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
    setPendingUser({ id: `optimistic-${Date.now()}`, role: "user", content: trimmed });
    send.mutate(trimmed);
  }

  function handleRetry(id: string) {
    if (pendingUser?.id !== id) return;
    const content = pendingUser.content;
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

  // Titles for the referenced-recipe chips: `cookbook-qa`'s response gives
  // ids only (see `CookbookQaOut.referenced_recipe_ids`), and there's no
  // single always-loaded `["recipes"]` cache to read from here — CookbookPage's
  // own list is cached per active filter set (`["recipes", filters]`), not a
  // stable key this component could reliably probe. Instead this fetches
  // each referenced id individually via the SAME `["recipe", id]` query key
  // `RecipeDetailPage` uses, so the two share a cache: a recipe visited (or
  // about to be visited via a chip click) before/after asking about it in
  // Q&A is fetched once, not twice.
  const recipeQueries = useQueries({
    queries: referencedRecipeIds.map((recipeId) => ({
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

  const sources =
    referencedRecipeIds.length > 0 ? (
      <div className="ai-panel__sources">
        <span className="ai-panel__sources-label">Recipes referenced in this answer</span>
        <ul className="ai-panel__sources-list">
          {referencedRecipeIds.map((recipeId, index) => {
            const query = recipeQueries[index];
            if (query.isError) return null;
            return (
              <li key={recipeId}>
                <Link to={`/recipes/${recipeId}`} className="chip chip--link">
                  {query.data?.title ?? "…"}
                </Link>
              </li>
            );
          })}
        </ul>
      </div>
    ) : null;

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
          >
            {sources}
          </ChatPanel>
        </div>
      ) : null}
    </section>
  );
}
