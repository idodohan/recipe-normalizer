import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { apiErrorEnvelope, apiErrorMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";
import { useCookbooks } from "../../hooks/useCookbooks";
import type { CookbookSummary } from "../../hooks/useCookbooks";
import { Button } from "../Button";
import { Dialog } from "../Dialog";
import { Field } from "../Field";
import "./save-to-cookbooks.css";

type SaveToCookbooksProps = {
  recipeId: string;
  /** Same owner check RecipeDetailPage already computes (`user.id === recipe.owner_id`). */
  isOwner: boolean;
};

/** Only cookbooks the caller can actually place a recipe into — placement
 *  requires editor+ on the cookbook (see the boards spec's access model). */
function actionable(cookbooks: CookbookSummary[] | undefined): CookbookSummary[] {
  return (cookbooks ?? []).filter((c) => c.role === "owner" || c.role === "editor");
}

/**
 * "Save to cookbooks" — replaces the old CollectionsControl. Owning a
 * recipe gets a multi-select checklist of the caller's cookbooks (a
 * placement, not a copy); viewing someone else's readable recipe gets a
 * single "save a copy into one of mine" action, since cross-owner saves
 * are copy-on-save (see cookbook.service.save_recipe_to_cookbook).
 */
export function SaveToCookbooks({ recipeId, isOwner }: SaveToCookbooksProps) {
  const [open, setOpen] = useState(false);

  const placements = useQuery({
    queryKey: ["recipe-cookbooks", recipeId],
    enabled: isOwner,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes/{recipe_id}/cookbooks", {
        params: { path: { recipe_id: recipeId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const count = placements.data?.length ?? 0;

  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>
        {isOwner ? `Cookbooks${count > 0 ? ` (${count})` : ""}` : "Save to my cookbook"}
      </Button>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title={isOwner ? "Save to cookbooks" : "Save to my cookbook"}
      >
        {isOwner ? (
          <OwnerChecklist recipeId={recipeId} />
        ) : (
          <SaveCopyPicker recipeId={recipeId} onSaved={() => setOpen(false)} />
        )}
      </Dialog>
    </>
  );
}

/** Owner view: checklist of my cookbooks, checked = this recipe is placed there. */
function OwnerChecklist({ recipeId }: { recipeId: string }) {
  const queryClient = useQueryClient();
  const cookbooks = useCookbooks();
  const placements = useQuery({
    queryKey: ["recipe-cookbooks", recipeId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes/{recipe_id}/cookbooks", {
        params: { path: { recipe_id: recipeId } },
      });
      if (error) throw error;
      return data;
    },
  });
  // Which row just got blocked by the backend's last-placement guard — the
  // checkbox itself stays checked (nothing changed), this just annotates it.
  const [blockedId, setBlockedId] = useState<string | null>(null);

  function invalidateAfterPlacementChange() {
    return Promise.all([
      queryClient.invalidateQueries({ queryKey: ["recipe-cookbooks", recipeId] }),
      queryClient.invalidateQueries({ queryKey: ["cookbooks"] }),
    ]);
  }

  const place = useMutation({
    mutationFn: async (cookbookId: string) => {
      const { error } = await api.POST("/api/recipes/{recipe_id}/cookbooks", {
        params: { path: { recipe_id: recipeId } },
        body: { cookbook_id: cookbookId },
      });
      if (error) throw error;
    },
    onSettled: () => invalidateAfterPlacementChange(),
    onError: () => {
      toast({
        title: "Could not save to that cookbook",
        description: "Your change was not saved. Please try again.",
        variant: "error",
      });
    },
  });

  const remove = useMutation({
    mutationFn: async (cookbookId: string) => {
      const { error } = await api.DELETE("/api/recipes/{recipe_id}/cookbooks/{cookbook_id}", {
        params: { path: { recipe_id: recipeId, cookbook_id: cookbookId } },
      });
      if (error) throw error;
    },
    onSuccess: () => invalidateAfterPlacementChange(),
    onError: (error, cookbookId) => {
      const { code } = apiErrorEnvelope(error);
      if (code === "last_placement") {
        setBlockedId(cookbookId);
        return;
      }
      toast({
        title: "Could not remove from that cookbook",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  // Same serialization CollectionsControl used: only one toggle in flight at
  // a time, so a later settle can't race an earlier one's cache write.
  const busy = place.isPending || remove.isPending;

  function toggle(cookbookId: string, checked: boolean) {
    setBlockedId(null);
    if (busy) return;
    if (checked) place.mutate(cookbookId);
    else remove.mutate(cookbookId);
  }

  if (cookbooks.isPending || placements.isPending) {
    return <p className="stc__status">Loading…</p>;
  }

  if (cookbooks.isError || placements.isError) {
    return (
      <p className="stc__status stc__status--error">
        {apiErrorMessage(cookbooks.error ?? placements.error, "Could not load your cookbooks.")}
      </p>
    );
  }

  const items = actionable(cookbooks.data);
  const placedIds = new Set((placements.data ?? []).map((c) => c.id));

  if (items.length === 0) {
    return <p className="stc__status">You don't have a cookbook to save this into yet.</p>;
  }

  return (
    <ul className="stc__list">
      {items.map((cookbook) => (
        <li key={cookbook.id} className="stc__item">
          <label className="stc__row">
            <input
              type="checkbox"
              checked={placedIds.has(cookbook.id)}
              disabled={busy}
              onChange={(event) => toggle(cookbook.id, event.target.checked)}
            />
            <span className="stc__name">{cookbook.name}</span>
            <span className="stc__count">{cookbook.recipe_count}</span>
          </label>
          {blockedId === cookbook.id ? (
            <p className="stc__note">
              A recipe has to live in at least one cookbook — add it to another first,
              or delete the recipe.
            </p>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** Non-owner view: pick one of my cookbooks; the backend copies the recipe in. */
function SaveCopyPicker({
  recipeId,
  onSaved,
}: {
  recipeId: string;
  onSaved: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const cookbooks = useCookbooks();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const items = actionable(cookbooks.data);

  // Default to the first actionable cookbook once the list loads, so the
  // picker isn't a dead select the user has to open before it does anything.
  useEffect(() => {
    if (!selectedId && items.length > 0) {
      // Reseeding from a query that just settled, not a derived-state sync.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSelectedId(items[0].id);
    }
  }, [items, selectedId]);

  const save = useMutation({
    mutationFn: async (cookbookId: string) => {
      const { data, error } = await api.POST("/api/recipes/{recipe_id}/cookbooks", {
        params: { path: { recipe_id: recipeId } },
        body: { cookbook_id: cookbookId },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: async (data, cookbookId) => {
      const cookbookName = items.find((c) => c.id === cookbookId)?.name ?? "your cookbook";
      await queryClient.invalidateQueries({ queryKey: ["cookbooks"] });
      onSaved();
      toast({ title: `Saved to ${cookbookName}`, variant: "success" });
      navigate(`/recipes/${data.recipe_id}`);
    },
    onError: (error) => {
      toast({
        title: "Could not save this recipe",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  if (cookbooks.isPending) {
    return <p className="stc__status">Loading…</p>;
  }

  if (cookbooks.isError) {
    return (
      <p className="stc__status stc__status--error">
        {apiErrorMessage(cookbooks.error, "Could not load your cookbooks.")}
      </p>
    );
  }

  if (items.length === 0) {
    return <p className="stc__status">Create a cookbook first, then come back to save this.</p>;
  }

  return (
    <form
      className="stc__picker"
      onSubmit={(event) => {
        event.preventDefault();
        if (selectedId && !save.isPending) save.mutate(selectedId);
      }}
    >
      <p className="stc__hint">
        Saves an independent copy into the cookbook you pick — future edits on either
        side stay separate.
      </p>
      <Field label="Cookbook">
        {(fieldProps) => (
          <select
            {...fieldProps}
            className="input"
            value={selectedId ?? ""}
            disabled={save.isPending}
            onChange={(event) => setSelectedId(event.target.value)}
          >
            {items.map((cookbook) => (
              <option key={cookbook.id} value={cookbook.id}>
                {cookbook.name}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Button type="submit" disabled={!selectedId || save.isPending}>
        {save.isPending ? "Saving…" : "Save"}
      </Button>
    </form>
  );
}
