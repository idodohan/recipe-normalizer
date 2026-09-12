import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Button } from "../components/Button";
import { Dialog } from "../components/Dialog";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Field, Input } from "../components/Field";
import { GeneratedCover } from "../components/GeneratedCover";
import { RecipeCard } from "../components/recipe/RecipeCard";
import { Skeleton } from "../components/Skeleton";
import { apiErrorMessage } from "../api/errors";
import { toast } from "../hooks/useToast";
import {
  useCookbook,
  useDeleteCookbook,
  useInviteMember,
  usePatchCookbook,
  useRemoveMember,
} from "../hooks/useCookbooks";
import type { CookbookDetail, Visibility } from "../hooks/useCookbooks";
import type { components } from "../api/schema";
import "./cookbook-detail.css";

type CookbookMemberOut = components["schemas"]["CookbookMemberOut"];

const VISIBILITY_OPTIONS: Array<{ value: Visibility; label: string }> = [
  { value: "private", label: "Private" },
  { value: "unlisted", label: "Shared by link" },
  { value: "public", label: "Public" },
];

const VISIBILITY_LABEL: Record<string, string> = {
  private: "Private",
  unlisted: "Shared by link",
  public: "Public",
};

export function CookbookDetailPage() {
  const { id } = useParams<{ id: string }>();
  const cookbook = useCookbook(id);

  if (cookbook.isPending) {
    return <Skeleton variant="card-grid" />;
  }

  if (cookbook.isError) {
    return (
      <ErrorState
        message={apiErrorMessage(cookbook.error, "Could not load this cookbook.")}
        onRetry={() => void cookbook.refetch()}
      />
    );
  }

  const data = cookbook.data;
  const isOwner = data.role === "owner";
  const canAddRecipe = data.role !== "viewer";

  return (
    <>
      <Link to="/" className="cbd__back">
        ← Cookbooks
      </Link>

      <Masthead cookbook={data} isOwner={isOwner} canAddRecipe={canAddRecipe} />

      {isOwner ? <OwnerPanel cookbook={data} /> : <ViewerBadge cookbook={data} />}

      <RecipeGrid cookbook={data} canAddRecipe={canAddRecipe} />
    </>
  );
}

function Masthead({
  cookbook,
  isOwner,
  canAddRecipe,
}: {
  cookbook: CookbookDetail;
  isOwner: boolean;
  canAddRecipe: boolean;
}) {
  const navigate = useNavigate();
  const [editing, setEditing] = useState(false);

  return (
    <header className="cbd__masthead">
      <div className="cbd__cover">
        {cookbook.cover_image_ref ? (
          <img
            className="cbd__cover-img"
            src={cookbook.cover_image_ref}
            alt=""
            loading="lazy"
          />
        ) : (
          <GeneratedCover seed={cookbook.id} label={cookbook.name} />
        )}
      </div>

      <div className="cbd__masthead-body">
        {isOwner && editing ? (
          <DetailsEditor cookbook={cookbook} onDone={() => setEditing(false)} />
        ) : (
          <div className="cbd__masthead-text">
            <p className="cbd__overline">Cookbook</p>
            <div className="cbd__title-row">
              <h1 className="cbd__title">{cookbook.name}</h1>
              {isOwner ? (
                <button
                  type="button"
                  className="cbd__edit-btn"
                  onClick={() => setEditing(true)}
                >
                  Edit
                </button>
              ) : null}
            </div>
            {cookbook.description ? (
              <p className="cbd__description">{cookbook.description}</p>
            ) : null}
            <p className="cbd__count">
              {cookbook.recipe_count}{" "}
              {cookbook.recipe_count === 1 ? "recipe" : "recipes"}
            </p>
          </div>
        )}

        {canAddRecipe ? (
          <div className="cbd__masthead-actions">
            <Button onClick={() => navigate("/add")}>Add a recipe</Button>
          </div>
        ) : null}
      </div>
    </header>
  );
}

function DetailsEditor({
  cookbook,
  onDone,
}: {
  cookbook: CookbookDetail;
  onDone: () => void;
}) {
  const patch = usePatchCookbook(cookbook.id);
  const [name, setName] = useState(cookbook.name);
  const [description, setDescription] = useState(cookbook.description ?? "");
  const [error, setError] = useState<string | null>(null);

  function submit() {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Give your cookbook a name.");
      return;
    }
    setError(null);
    patch.mutate(
      { name: trimmed, description: description.trim() || null },
      {
        onSuccess: () => onDone(),
        onError: (e) => setError(apiErrorMessage(e, "Could not save changes.")),
      },
    );
  }

  return (
    <div className="cbd__edit-form">
      <Field label="Cookbook name" error={error ?? undefined}>
        {(props) => (
          <Input
            {...props}
            value={name}
            autoFocus
            invalid={Boolean(error)}
            onChange={(e) => {
              setName(e.target.value);
              setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                onDone();
              }
            }}
          />
        )}
      </Field>
      <Field label="Description">
        {(props) => (
          <textarea
            {...props}
            className="input cbd__textarea"
            rows={2}
            value={description}
            placeholder="What's this cookbook for?"
            onChange={(e) => setDescription(e.target.value)}
          />
        )}
      </Field>
      <div className="cbd__edit-form-actions">
        <Button variant="secondary" onClick={onDone} disabled={patch.isPending}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={patch.isPending}>
          {patch.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}

function ViewerBadge({ cookbook }: { cookbook: CookbookDetail }) {
  return (
    <div className="cbd__viewer-row">
      <span className="cbd-badge">
        {VISIBILITY_LABEL[cookbook.visibility] ?? cookbook.visibility}
      </span>
      <span className="cbd-badge cbd-badge--role">Your role · {cookbook.role}</span>
    </div>
  );
}

function OwnerPanel({ cookbook }: { cookbook: CookbookDetail }) {
  return (
    <section className="cbd__owner-panel">
      <VisibilityControl cookbook={cookbook} />
      <MembersPanel cookbookId={cookbook.id} />
      <DangerZone cookbook={cookbook} />
    </section>
  );
}

function VisibilityControl({ cookbook }: { cookbook: CookbookDetail }) {
  const patch = usePatchCookbook(cookbook.id);
  const [copied, setCopied] = useState(false);

  function setVisibility(visibility: Visibility) {
    if (visibility === cookbook.visibility || patch.isPending) return;
    patch.mutate(
      { visibility },
      {
        onError: (e) =>
          toast({
            title: "Could not change visibility",
            description: apiErrorMessage(e, "Please try again."),
            variant: "error",
          }),
      },
    );
  }

  const showShare = cookbook.visibility !== "private" && Boolean(cookbook.public_token);

  async function handleCopy() {
    if (!cookbook.public_token) return;
    try {
      await navigator.clipboard.writeText(cookbook.public_token);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({
        title: "Could not copy",
        description: "Select the token text and copy it manually.",
        variant: "error",
      });
    }
  }

  return (
    <div className="cbd__panel-block">
      <h2 className="cbd__panel-heading">Visibility</h2>
      <div className="cbd-seg" role="group" aria-label="Cookbook visibility">
        {VISIBILITY_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            aria-pressed={cookbook.visibility === opt.value}
            className={
              cookbook.visibility === opt.value
                ? "cbd-seg__option cbd-seg__option--active"
                : "cbd-seg__option"
            }
            disabled={patch.isPending}
            onClick={() => setVisibility(opt.value)}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {showShare ? (
        <div className="cbd__share">
          <Field label="Share token">
            {(props) => (
              <div className="cbd__share-row">
                <Input
                  {...props}
                  readOnly
                  value={cookbook.public_token ?? ""}
                  onFocus={(e) => e.currentTarget.select()}
                />
                <Button variant="secondary" onClick={() => void handleCopy()}>
                  {copied ? "Copied" : "Copy"}
                </Button>
              </div>
            )}
          </Field>
          <p className="cbd__share-note">
            Public cookbook pages arrive with the next update — for now, this
            token identifies your cookbook for sharing behind the scenes.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function MembersPanel({ cookbookId }: { cookbookId: string }) {
  const invite = useInviteMember(cookbookId);
  const remove = useRemoveMember(cookbookId);
  // The backend has no GET-members-list endpoint (only invite/change-role/
  // remove), and CookbookDetailOut carries no `members` field — so there is
  // no way to show a cookbook's existing membership on page load. This
  // tracks members invited during the current visit only, from the invite
  // mutation's response, which is the one place membership data reaches the
  // client at all today.
  const [members, setMembers] = useState<CookbookMemberOut[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"editor" | "viewer">("editor");
  const [error, setError] = useState<string | null>(null);

  function handleInvite(event: FormEvent) {
    event.preventDefault();
    const trimmed = email.trim();
    if (!trimmed || invite.isPending) return;
    setError(null);
    invite.mutate(
      { email: trimmed, role },
      {
        onSuccess: (member) => {
          setMembers((prev) => {
            const next = prev.filter((m) => m.user_id !== member.user_id);
            return [...next, member];
          });
          setEmail("");
          toast({ title: `Invited ${trimmed}`, variant: "success" });
        },
        onError: (e) => setError(apiErrorMessage(e, "Could not send invite.")),
      },
    );
  }

  function handleRemove(userId: string) {
    remove.mutate(userId, {
      onSuccess: () => setMembers((prev) => prev.filter((m) => m.user_id !== userId)),
      onError: (e) =>
        toast({
          title: "Could not remove member",
          description: apiErrorMessage(e, "Please try again."),
          variant: "error",
        }),
    });
  }

  return (
    <div className="cbd__panel-block">
      <h2 className="cbd__panel-heading">Members</h2>
      <p className="cbd__hint">
        People you invite during this visit appear below — viewing a cookbook's
        full membership isn't available yet.
      </p>

      {members.length > 0 ? (
        <ul className="cbd-members">
          {members.map((m) => (
            <li key={m.user_id} className="cbd-members__row">
              <span className="cbd-members__id">Member · {m.user_id.slice(0, 8)}</span>
              <span className="cbd-badge cbd-badge--role">{m.role}</span>
              <button
                type="button"
                className="cbd-members__remove"
                aria-label="Remove member"
                disabled={remove.isPending}
                onClick={() => handleRemove(m.user_id)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <form className="cbd__invite" onSubmit={handleInvite}>
        <Field label="Invite by email" error={error ?? undefined}>
          {(props) => (
            <Input
              {...props}
              type="email"
              value={email}
              placeholder="friend@example.com"
              invalid={Boolean(error)}
              onChange={(e) => {
                setEmail(e.target.value);
                setError(null);
              }}
            />
          )}
        </Field>
        <label className="cbd__role-select-label">
          Role
          <select
            className="cbd__role-select"
            value={role}
            onChange={(e) => setRole(e.target.value as "editor" | "viewer")}
          >
            <option value="editor">Editor</option>
            <option value="viewer">Viewer</option>
          </select>
        </label>
        <Button type="submit" disabled={invite.isPending}>
          {invite.isPending ? "Inviting…" : "Invite"}
        </Button>
      </form>
    </div>
  );
}

function DangerZone({ cookbook }: { cookbook: CookbookDetail }) {
  const navigate = useNavigate();
  const del = useDeleteCookbook();
  const [confirmOpen, setConfirmOpen] = useState(false);

  if (cookbook.is_default) return null;

  function handleDelete() {
    del.mutate(cookbook.id, {
      onSuccess: () => navigate("/"),
      onError: (e) => {
        setConfirmOpen(false);
        toast({
          title: "Could not delete cookbook",
          description: apiErrorMessage(e, "Please try again."),
          variant: "error",
        });
      },
    });
  }

  return (
    <div className="cbd__panel-block cbd__panel-block--danger">
      <h2 className="cbd__panel-heading">Delete this cookbook</h2>
      <p className="cbd__hint">
        Removes the cookbook for everyone it's shared with. Recipes inside it are
        not deleted.
      </p>
      <Button variant="danger" onClick={() => setConfirmOpen(true)}>
        Delete cookbook
      </Button>

      <Dialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="Delete cookbook?"
      >
        <div className="dialog__body">
          <p>
            Delete <strong>{cookbook.name}</strong>? This can't be undone.
          </p>
        </div>
        <div className="cbd__dialog-actions">
          <Button
            variant="secondary"
            onClick={() => setConfirmOpen(false)}
            disabled={del.isPending}
          >
            Cancel
          </Button>
          <Button variant="danger" onClick={handleDelete} disabled={del.isPending}>
            {del.isPending ? "Deleting…" : "Delete cookbook"}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}

function RecipeGrid({
  cookbook,
  canAddRecipe,
}: {
  cookbook: CookbookDetail;
  canAddRecipe: boolean;
}) {
  const navigate = useNavigate();
  const [filter, setFilter] = useState("");

  if (cookbook.recipes.length === 0) {
    return (
      <EmptyState
        title="Your cookbook is empty"
        body="Welcome! This app helps you organize and scale your recipes. Paste a link, drop a PDF or photo, or type a recipe in — we normalize every amount to grams and millilitres so you can cook with confidence."
        action={
          canAddRecipe ? (
            <Button onClick={() => navigate("/add")}>Add your first recipe</Button>
          ) : undefined
        }
      />
    );
  }

  // Within-cookbook search: a light client-side filter by title over the
  // recipes this cookbook already loaded. Cross-cookbook full-text and AI
  // search live on the Cookbooks home.
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? cookbook.recipes.filter((r) => r.title.toLowerCase().includes(needle))
    : cookbook.recipes;

  return (
    <section className="cbd__recipes">
      {cookbook.recipes.length > 4 ? (
        <input
          type="search"
          className="cbd__search"
          placeholder={`Search in ${cookbook.name}…`}
          aria-label={`Search recipes in ${cookbook.name}`}
          value={filter}
          maxLength={200}
          onChange={(e) => setFilter(e.target.value)}
        />
      ) : null}

      {shown.length === 0 ? (
        <p className="cbd__no-match">No recipes in this cookbook match “{filter}”.</p>
      ) : (
        <ul className="cbd-grid">
          {shown.map((recipe, index) => (
            <li key={recipe.id}>
              <RecipeCard recipe={recipe} index={index} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
