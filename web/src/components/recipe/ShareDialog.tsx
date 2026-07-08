import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { apiErrorEnvelope, apiErrorMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";
import { Dialog } from "../Dialog";
import "./share-dialog.css";

type ShareDialogProps = {
  open: boolean;
  onClose: () => void;
  recipeId: string;
};

type AddState = "idle" | "pending" | "added" | "already";

/**
 * The recipe's "Share" dialog — owner-only. Three independent sections,
 * each with its own query/mutations so one failing doesn't block another:
 * copy-on-share by email, a revocable public link, and adding the recipe
 * to one of the owner's shared cookbooks (with inline create-new).
 */
export function ShareDialog({ open, onClose, recipeId }: ShareDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} title="Share this recipe">
      <div className="share-dlg">
        <SendCopySection recipeId={recipeId} />
        <PublicLinkSection recipeId={recipeId} open={open} />
        <SharedCookbookSection recipeId={recipeId} open={open} />
      </div>
    </Dialog>
  );
}

function SendCopySection({ recipeId }: { recipeId: string }) {
  const [email, setEmail] = useState("");
  const [inlineError, setInlineError] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: async (toEmail: string) => {
      const { data, error } = await api.POST("/api/share/recipe", {
        body: { recipe_id: recipeId, to_email: toEmail },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      setEmail("");
      setInlineError(null);
      toast({ title: `Sent to ${data.to_email}`, variant: "success" });
    },
    onError: (error) => {
      const { code } = apiErrorEnvelope(error);
      if (code === "recipient_not_found") {
        setInlineError("No account with that email.");
        return;
      }
      setInlineError(null);
      toast({
        title: "Could not send",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = email.trim();
    if (!trimmed || send.isPending) return;
    send.mutate(trimmed);
  }

  return (
    <section className="share-dlg__section">
      <h3 className="share-dlg__heading">Send a copy</h3>
      <p className="share-dlg__hint">
        Sends a standalone copy to their cookbook — future edits on either side stay
        independent.
      </p>
      <form className="share-dlg__row" onSubmit={handleSubmit}>
        <input
          type="email"
          className="input share-dlg__input"
          placeholder="friend@example.com"
          aria-label="Recipient email"
          value={email}
          onChange={(event) => {
            setEmail(event.target.value);
            setInlineError(null);
          }}
          required
        />
        <button type="submit" className="btn btn--secondary btn--sm" disabled={send.isPending}>
          {send.isPending ? "Sending…" : "Send"}
        </button>
      </form>
      {inlineError ? (
        <p className="share-dlg__error" role="alert">
          {inlineError}
        </p>
      ) : null}
    </section>
  );
}

function PublicLinkSection({ recipeId, open }: { recipeId: string; open: boolean }) {
  const queryClient = useQueryClient();
  const [copied, setCopied] = useState(false);
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);

  const links = useQuery({
    queryKey: ["share", "public-links"],
    enabled: open,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/share/public");
      if (error) throw error;
      return data;
    },
  });

  const link = (links.data ?? []).find(
    (candidate) => candidate.recipe_id === recipeId && !candidate.revoked_at,
  );

  const create = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/share/public", {
        body: { recipe_id: recipeId },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["share", "public-links"] });
    },
    onError: (error) => {
      toast({
        title: "Could not create link",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  const revoke = useMutation({
    mutationFn: async (linkId: string) => {
      const { error } = await api.DELETE("/api/share/public/{link_id}", {
        params: { path: { link_id: linkId } },
      });
      if (error) throw error;
    },
    onSuccess: async () => {
      setConfirmingRevoke(false);
      await queryClient.invalidateQueries({ queryKey: ["share", "public-links"] });
      toast({ title: "Link revoked", variant: "success" });
    },
    onError: (error) => {
      setConfirmingRevoke(false);
      toast({
        title: "Could not revoke link",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  const url = link ? `${window.location.origin}/p/${link.token}` : null;

  async function handleCopy() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied (permissions, insecure context) —
      // fall back to a toast rather than leaving the click looking inert.
      toast({
        title: "Could not copy link",
        description: "Select the link text and copy it manually.",
        variant: "error",
      });
    }
  }

  return (
    <section className="share-dlg__section">
      <h3 className="share-dlg__heading">Public link</h3>
      <p className="share-dlg__hint">Anyone with the link can view this recipe — no account needed.</p>

      {links.isPending ? (
        <p className="share-dlg__status">Loading…</p>
      ) : links.isError ? (
        <p className="share-dlg__error" role="alert">
          {apiErrorMessage(links.error, "Could not load your public links.")}
        </p>
      ) : url ? (
        <>
          <div className="share-dlg__row">
            <input
              type="text"
              className="input share-dlg__input"
              readOnly
              value={url}
              aria-label="Public link"
              onFocus={(event) => event.currentTarget.select()}
            />
            <button type="button" className="btn btn--secondary btn--sm" onClick={() => void handleCopy()}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          {confirmingRevoke ? (
            <span className="share-dlg__confirm">
              <span className="share-dlg__confirm-q">Revoke this link?</span>
              <button
                type="button"
                className="share-dlg__text-btn share-dlg__text-btn--danger"
                disabled={revoke.isPending}
                onClick={() => link && revoke.mutate(link.id)}
              >
                {revoke.isPending ? "Revoking…" : "Yes"}
              </button>
              <button
                type="button"
                className="share-dlg__text-btn"
                disabled={revoke.isPending}
                onClick={() => setConfirmingRevoke(false)}
              >
                No
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="share-dlg__text-btn share-dlg__text-btn--danger"
              onClick={() => setConfirmingRevoke(true)}
            >
              Revoke
            </button>
          )}
        </>
      ) : (
        <button
          type="button"
          className="btn btn--secondary btn--sm"
          disabled={create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "Creating…" : "Create link"}
        </button>
      )}
    </section>
  );
}

function SharedCookbookSection({ recipeId, open }: { recipeId: string; open: boolean }) {
  const queryClient = useQueryClient();
  const [newName, setNewName] = useState("");
  const [status, setStatus] = useState<Record<string, AddState>>({});

  const cookbooks = useQuery({
    queryKey: ["shared-cookbooks"],
    enabled: open,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/shared-cookbooks");
      if (error) throw error;
      return data;
    },
  });

  const addTo = useMutation({
    mutationFn: async (cookbookId: string) => {
      const { error } = await api.POST("/api/shared-cookbooks/{cookbook_id}/recipes", {
        params: { path: { cookbook_id: cookbookId } },
        body: { recipe_id: recipeId },
      });
      if (error) throw error;
      return cookbookId;
    },
    onMutate: (cookbookId) => {
      setStatus((prev) => ({ ...prev, [cookbookId]: "pending" }));
    },
    onSuccess: (cookbookId) => {
      setStatus((prev) => ({ ...prev, [cookbookId]: "added" }));
    },
    onError: (error, cookbookId) => {
      const { code } = apiErrorEnvelope(error);
      if (code === "already_added") {
        setStatus((prev) => ({ ...prev, [cookbookId]: "already" }));
        return;
      }
      setStatus((prev) => {
        const next = { ...prev };
        delete next[cookbookId];
        return next;
      });
      toast({
        title: "Could not add recipe",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  const create = useMutation({
    mutationFn: async (name: string) => {
      const { data, error } = await api.POST("/api/shared-cookbooks", { body: { name } });
      if (error) throw error;
      return data;
    },
    onSuccess: async (created) => {
      setNewName("");
      await queryClient.invalidateQueries({ queryKey: ["shared-cookbooks"] });
      // Mirrors CollectionsControl's auto-add-on-create — a freshly made
      // cookbook is almost always created *for* the recipe you're sharing.
      addTo.mutate(created.id);
    },
    onError: (error) => {
      toast({
        title: "Could not create shared cookbook",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  function handleCreate(event: FormEvent) {
    event.preventDefault();
    const trimmed = newName.trim();
    if (!trimmed || create.isPending) return;
    create.mutate(trimmed);
  }

  const items = cookbooks.data ?? [];

  return (
    <section className="share-dlg__section">
      <h3 className="share-dlg__heading">Add to a shared cookbook</h3>

      {cookbooks.isPending ? (
        <p className="share-dlg__status">Loading…</p>
      ) : cookbooks.isError ? (
        <p className="share-dlg__error" role="alert">
          {apiErrorMessage(cookbooks.error, "Could not load your shared cookbooks.")}
        </p>
      ) : items.length === 0 ? (
        <p className="share-dlg__status">No shared cookbooks yet — start one below.</p>
      ) : (
        <ul className="share-dlg__cookbook-list">
          {items.map((cookbook) => {
            const state = status[cookbook.id] ?? "idle";
            return (
              <li key={cookbook.id} className="share-dlg__cookbook-item">
                <span className="share-dlg__cookbook-name">{cookbook.name}</span>
                <button
                  type="button"
                  className="share-dlg__text-btn share-dlg__text-btn--primary"
                  disabled={state !== "idle"}
                  onClick={() => addTo.mutate(cookbook.id)}
                >
                  {state === "pending"
                    ? "Adding…"
                    : state === "added"
                      ? "Added"
                      : state === "already"
                        ? "Already in"
                        : "Add"}
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <form className="share-dlg__create-form" onSubmit={handleCreate}>
        <label className="share-dlg__create-label" htmlFor="share-dlg-new-cookbook">
          New shared cookbook
        </label>
        <div className="share-dlg__row">
          <input
            id="share-dlg-new-cookbook"
            className="input share-dlg__input"
            placeholder="e.g. Sunday Dinners"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
          />
          <button
            type="submit"
            className="btn btn--secondary btn--sm"
            disabled={!newName.trim() || create.isPending}
          >
            {create.isPending ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </section>
  );
}
