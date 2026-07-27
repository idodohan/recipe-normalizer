import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Skeleton } from "../components/Skeleton";
import { toast } from "../hooks/useToast";
import { useUser } from "../hooks/useUser";
import type { components } from "../api/schema";
import "./shared-cookbook.css";

type SharedCookbookRecipe = components["schemas"]["SharedCookbookRecipeOut"];

/** "3 min", "45 min", "1 hr 30 min" — same shorthand as RecipeCard/RecipeDetailPage. */
function formatTotalTime(totalMin: number | null | undefined): string | null {
  if (totalMin == null || totalMin <= 0) return null;
  if (totalMin < 60) return `${totalMin} min`;
  const hours = Math.floor(totalMin / 60);
  const minutes = totalMin % 60;
  return minutes === 0 ? `${hours} hr` : `${hours} hr ${minutes} min`;
}

export function SharedCookbookPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useUser();

  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [leaveError, setLeaveError] = useState<string | null>(null);
  const [confirmingLeave, setConfirmingLeave] = useState(false);
  const [confirmingRemove, setConfirmingRemove] = useState<string | null>(null);
  const [addRecipeId, setAddRecipeId] = useState("");

  const cookbook = useQuery({
    queryKey: ["shared-cookbook", id],
    enabled: Boolean(id),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/shared-cookbooks/{cookbook_id}", {
        params: { path: { cookbook_id: id! } },
      });
      if (error) throw error;
      return data;
    },
  });

  // "My recipes not already in this cookbook" — the simpler of the two
  // reasonable data sources: GET /api/recipes only ever returns recipes I
  // OWN (see cookbook.service.list_recipes), so this is exactly "recipes I
  // could add" without a second access-check round trip.
  const myRecipes = useQuery({
    queryKey: ["recipes", "mine-for-add"],
    enabled: Boolean(id),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes", {
        params: { query: { limit: 200, offset: 0 } },
      });
      if (error) throw error;
      return data;
    },
  });

  function invalidateCookbook() {
    return queryClient.invalidateQueries({ queryKey: ["shared-cookbook", id] });
  }

  const invite = useMutation({
    mutationFn: async (email: string) => {
      const { data, error } = await api.POST("/api/shared-cookbooks/{cookbook_id}/members", {
        params: { path: { cookbook_id: id! } },
        body: { email },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: async () => {
      setInviteEmail("");
      setInviteError(null);
      await invalidateCookbook();
    },
    onError: (error) => {
      setInviteError(apiErrorMessage(error, "Could not invite this person."));
    },
  });

  const removeMember = useMutation({
    mutationFn: async (userId: string) => {
      const { error } = await api.DELETE("/api/shared-cookbooks/{cookbook_id}/members/{user_id}", {
        params: { path: { cookbook_id: id!, user_id: userId } },
      });
      if (error) throw error;
      return userId;
    },
    onSuccess: async (userId) => {
      setConfirmingRemove(null);
      const isSelf = user?.id === userId;
      if (isSelf) {
        navigate("/shares");
        return;
      }
      await invalidateCookbook();
    },
    onError: (error, userId) => {
      const isSelf = user?.id === userId;
      if (isSelf) {
        setLeaveError(apiErrorMessage(error, "Could not leave this shared cookbook."));
        setConfirmingLeave(false);
      } else {
        setConfirmingRemove(null);
        toast({
          title: "Could not remove this member",
          description: apiErrorMessage(error),
          variant: "error",
        });
      }
    },
  });

  const addRecipe = useMutation({
    mutationFn: async (recipeId: string) => {
      const { data, error } = await api.POST("/api/shared-cookbooks/{cookbook_id}/recipes", {
        params: { path: { cookbook_id: id! } },
        body: { recipe_id: recipeId },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: async () => {
      setAddRecipeId("");
      await invalidateCookbook();
    },
    onError: (error) => {
      toast({
        title: "Could not add this recipe",
        description: apiErrorMessage(error),
        variant: "error",
      });
    },
  });

  const removeRecipe = useMutation({
    mutationFn: async (recipeId: string) => {
      const { error } = await api.DELETE("/api/shared-cookbooks/{cookbook_id}/recipes/{recipe_id}", {
        params: { path: { cookbook_id: id!, recipe_id: recipeId } },
      });
      if (error) throw error;
    },
    onSuccess: () => invalidateCookbook(),
    onError: (error) => {
      toast({
        title: "Could not remove this recipe",
        description: apiErrorMessage(error),
        variant: "error",
      });
    },
  });

  function handleInvite(event: FormEvent) {
    event.preventDefault();
    const trimmed = inviteEmail.trim();
    if (!trimmed || invite.isPending) return;
    invite.mutate(trimmed);
  }

  function handleAddRecipe(event: FormEvent) {
    event.preventDefault();
    if (!addRecipeId || addRecipe.isPending) return;
    addRecipe.mutate(addRecipeId);
  }

  if (cookbook.isPending) {
    return <Skeleton variant="rows" count={4} />;
  }

  if (cookbook.isError || !cookbook.data) {
    return (
      <ErrorState
        message={apiErrorMessage(cookbook.error, "We couldn’t open this shared cookbook.")}
        onRetry={() => void cookbook.refetch()}
      />
    );
  }

  const data = cookbook.data;
  const isCreator = user?.id === data.created_by;
  const linkedIds = new Set(data.recipes.map((recipe) => recipe.id));
  const addableRecipes = (myRecipes.data?.items ?? []).filter((recipe) => !linkedIds.has(recipe.id));

  return (
    <article className="shc">
      <div className="shc__toolbar">
        <Link to="/shares" className="shc__back">
          ← Shares
        </Link>
      </div>

      <header className="shc__header">
        <p className="shc__kicker">Shared cookbook</p>
        <h1 className="shc__title">{data.name}</h1>
      </header>

      <div className="shc__body">
        <section className="shc__recipes">
          <h2 className="shc__colhead">Recipes</h2>

          {data.recipes.length === 0 ? (
            <EmptyState
              title="No recipes yet"
              body="Add one from the list on the right, or share a recipe into this cookbook from its detail page."
            />
          ) : (
            <ul className="shc__grid">
              {data.recipes.map((recipe) => (
                <SharedRecipeTile
                  key={recipe.id}
                  recipe={recipe}
                  onRemove={() => removeRecipe.mutate(recipe.id)}
                  removing={removeRecipe.isPending && removeRecipe.variables === recipe.id}
                />
              ))}
            </ul>
          )}

          <form className="shc__add-form" onSubmit={handleAddRecipe}>
            <label className="shc__add-label" htmlFor="shc-add-recipe">
              Add one of your recipes
            </label>
            <div className="shc__add-row">
              <select
                id="shc-add-recipe"
                className="input shc__add-select"
                value={addRecipeId}
                onChange={(event) => setAddRecipeId(event.target.value)}
                disabled={
                  myRecipes.isPending || myRecipes.isError || addableRecipes.length === 0
                }
              >
                {/* The error branch has to come before the empty one: a failed
                    fetch also yields zero addable recipes, and reporting that
                    as "all your recipes are already in this cookbook" is a
                    plain lie about the user's cookbook. */}
                <option value="">
                  {myRecipes.isPending
                    ? "Loading your recipes…"
                    : myRecipes.isError
                      ? "Could not load your recipes — reload to try again"
                      : addableRecipes.length === 0
                        ? "All your recipes are already in this cookbook"
                        : "Choose a recipe…"}
                </option>
                {addableRecipes.map((recipe) => (
                  <option key={recipe.id} value={recipe.id}>
                    {recipe.title}
                  </option>
                ))}
              </select>
              <Button
                type="submit"
                variant="secondary"
                disabled={!addRecipeId || addRecipe.isPending}
              >
                {addRecipe.isPending ? "Adding…" : "Add"}
              </Button>
            </div>
          </form>
        </section>

        <aside className="shc__members">
          <h2 className="shc__colhead">Members</h2>
          <ul className="shc__member-list">
            {data.members.map((member) => {
              const isSelf = member.user_id === user?.id;
              return (
                <li key={member.user_id} className="shc__member-item">
                  <span className="shc__member-name">
                    {member.display_name}
                    {member.is_creator ? <span className="shc__member-badge">Creator</span> : null}
                  </span>
                  {isSelf ? (
                    confirmingLeave ? (
                      <span className="shc__confirm">
                        <span className="shc__confirm-q">Leave?</span>
                        <button
                          type="button"
                          className="shc__text-btn shc__text-btn--danger"
                          disabled={removeMember.isPending}
                          onClick={() => removeMember.mutate(member.user_id)}
                        >
                          {removeMember.isPending ? "Leaving…" : "Yes"}
                        </button>
                        <button
                          type="button"
                          className="shc__text-btn"
                          disabled={removeMember.isPending}
                          onClick={() => setConfirmingLeave(false)}
                        >
                          No
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="shc__text-btn shc__text-btn--danger"
                        onClick={() => {
                          setLeaveError(null);
                          setConfirmingLeave(true);
                        }}
                      >
                        Leave
                      </button>
                    )
                  ) : isCreator ? (
                    confirmingRemove === member.user_id ? (
                      <span className="shc__confirm">
                        <span className="shc__confirm-q">Remove?</span>
                        <button
                          type="button"
                          className="shc__text-btn shc__text-btn--danger"
                          disabled={removeMember.isPending}
                          onClick={() => removeMember.mutate(member.user_id)}
                        >
                          {removeMember.isPending ? "Removing…" : "Yes"}
                        </button>
                        <button
                          type="button"
                          className="shc__text-btn"
                          disabled={removeMember.isPending}
                          onClick={() => setConfirmingRemove(null)}
                        >
                          No
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="shc__text-btn shc__text-btn--danger"
                        onClick={() => setConfirmingRemove(member.user_id)}
                      >
                        Remove
                      </button>
                    )
                  ) : null}
                </li>
              );
            })}
          </ul>

          {leaveError ? (
            <p className="shc__error" role="alert">
              {leaveError}
            </p>
          ) : null}

          <form className="shc__invite-form" onSubmit={handleInvite}>
            <label className="shc__add-label" htmlFor="shc-invite-email">
              Invite by email
            </label>
            <div className="shc__add-row">
              <input
                id="shc-invite-email"
                type="email"
                className="input shc__invite-input"
                placeholder="friend@example.com"
                value={inviteEmail}
                onChange={(event) => {
                  setInviteEmail(event.target.value);
                  setInviteError(null);
                }}
              />
              <Button type="submit" variant="secondary" disabled={!inviteEmail.trim() || invite.isPending}>
                {invite.isPending ? "Inviting…" : "Invite"}
              </Button>
            </div>
            {inviteError ? (
              <p className="shc__error" role="alert">
                {inviteError}
              </p>
            ) : null}
          </form>
        </aside>
      </div>
    </article>
  );
}

function SharedRecipeTile({
  recipe,
  onRemove,
  removing,
}: {
  recipe: SharedCookbookRecipe;
  onRemove: () => void;
  removing: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  const time = formatTotalTime(recipe.total_min);
  const initial = [...recipe.title.trim()][0]?.toUpperCase() ?? "—";

  return (
    <li className="shc-tile-shell">
      <Link to={`/recipes/${recipe.id}`} className="shc-tile">
        <p className="shc-tile__kicker">
          {recipe.dish_types.length > 0 ? recipe.dish_types.join(" · ") : "recipe"}
        </p>
        {recipe.image_ref ? (
          <img className="shc-tile__image" src={recipe.image_ref} alt="" loading="lazy" />
        ) : (
          <span className="shc-tile__initial" aria-hidden="true">
            {initial}
          </span>
        )}
        <h3 className="shc-tile__title">{recipe.title}</h3>
        <p className="shc-tile__foot">
          <span>{time ?? "—"}</span>
          {recipe.last_edited_by_name ? <span>Edited by {recipe.last_edited_by_name}</span> : null}
        </p>
      </Link>

      {confirming ? (
        <span className="shc-tile__confirm">
          <button
            type="button"
            className="shc__text-btn shc__text-btn--danger"
            disabled={removing}
            onClick={onRemove}
          >
            {removing ? "Removing…" : "Remove?"}
          </button>
          <button
            type="button"
            className="shc__text-btn"
            disabled={removing}
            onClick={() => setConfirming(false)}
          >
            No
          </button>
        </span>
      ) : (
        <button
          type="button"
          className="shc-tile__remove"
          aria-label={`Remove ${recipe.title} from this shared cookbook`}
          onClick={() => setConfirming(true)}
        >
          ×
        </button>
      )}
    </li>
  );
}
