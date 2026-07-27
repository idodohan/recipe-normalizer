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

/**
 * The recipe's "Share" dialog — owner-only. Two independent sections, each
 * with its own query/mutations so one failing doesn't block another:
 * copy-on-share by email and a revocable public link.
 *
 * The third section — "add to a shared cookbook" — is gone with the
 * shared-cookbook feature itself; co-owned cookbooks are now real Cookbooks,
 * shared by inviting members to them, not by pinning individual recipes.
 */
export function ShareDialog({ open, onClose, recipeId }: ShareDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} title="Share this recipe">
      <div className="share-dlg">
        <SendCopySection recipeId={recipeId} />
        <PublicLinkSection recipeId={recipeId} open={open} />
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
