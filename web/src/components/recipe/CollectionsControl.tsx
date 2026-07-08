import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { apiErrorMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";
import type { components } from "../../api/schema";
import "./collections-control.css";

type RecipeOut = components["schemas"]["RecipeOut"];
type CollectionOut = components["schemas"]["CollectionOut"];

type CollectionsControlProps = {
  recipeId: string;
  collectionIds: string[];
};

/**
 * "Collections" disclosure — a toggle button that opens a hairline-framed
 * panel anchored below it (the app has no shared popover primitive, so
 * this is a self-contained one: outside-click + Escape to close, no focus
 * trap since it's not modal). Lists the user's collections as checkboxes,
 * an inline "new collection" form, and a "Manage" mode for rename/delete.
 */
export function CollectionsControl({ recipeId, collectionIds }: CollectionsControlProps) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [manageMode, setManageMode] = useState(false);
  const [newName, setNewName] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const panelId = useId();
  const newCollectionInputId = `${panelId}-new`;

  const collections = useQuery({
    queryKey: ["collections"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/collections");
      if (error) throw error;
      return data;
    },
  });

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      const target = event.target as Node;
      if (panelRef.current?.contains(target) || buttonRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // Closing the panel also leaves manage mode / any in-progress
  // rename-or-delete confirm, so reopening always starts fresh.
  useEffect(() => {
    if (!open) {
      setManageMode(false);
      setRenamingId(null);
      setDeletingId(null);
      setNewName("");
    }
  }, [open]);

  function invalidateAfterAssignmentChange() {
    return Promise.all([
      queryClient.invalidateQueries({ queryKey: ["collections"] }),
      queryClient.invalidateQueries({ queryKey: ["recipes"] }),
    ]);
  }

  const setRecipeCollections = useMutation({
    mutationFn: async (nextIds: string[]) => {
      const { data, error } = await api.PUT("/api/recipes/{recipe_id}/collections", {
        params: { path: { recipe_id: recipeId } },
        body: { collection_ids: nextIds },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: async (data) => {
      queryClient.setQueryData<RecipeOut>(["recipe", recipeId], data);
      await invalidateAfterAssignmentChange();
    },
    onError: () => {
      toast({
        title: "Could not update collections",
        description: "Your change was not saved. Please try again.",
        variant: "error",
      });
    },
  });

  const createCollection = useMutation({
    mutationFn: async (name: string) => {
      const { data, error } = await api.POST("/api/collections", { body: { name } });
      if (error) throw error;
      return data;
    },
    onSuccess: async (created: CollectionOut) => {
      setNewName("");
      await queryClient.invalidateQueries({ queryKey: ["collections"] });
      // Auto-check the newly created collection for this recipe.
      setRecipeCollections.mutate([...collectionIds, created.id]);
    },
    onError: () => {
      toast({
        title: "Could not create collection",
        description: "Please try again.",
        variant: "error",
      });
    },
  });

  const renameCollection = useMutation({
    mutationFn: async ({ id, name }: { id: string; name: string }) => {
      const { data, error } = await api.PATCH("/api/collections/{collection_id}", {
        params: { path: { collection_id: id } },
        body: { name },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: async () => {
      setRenamingId(null);
      await queryClient.invalidateQueries({ queryKey: ["collections"] });
    },
    onError: () => {
      toast({
        title: "Could not rename collection",
        description: "Please try again.",
        variant: "error",
      });
    },
  });

  const deleteCollection = useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.DELETE("/api/collections/{collection_id}", {
        params: { path: { collection_id: id } },
      });
      if (error) throw error;
    },
    onSuccess: async () => {
      setDeletingId(null);
      await queryClient.invalidateQueries({ queryKey: ["recipe", recipeId] });
      await invalidateAfterAssignmentChange();
    },
    onError: () => {
      setDeletingId(null);
      toast({
        title: "Could not delete collection",
        description: "Please try again.",
        variant: "error",
      });
    },
  });

  function toggle(collectionId: string, checked: boolean) {
    const next = checked
      ? [...collectionIds, collectionId]
      : collectionIds.filter((existing) => existing !== collectionId);
    setRecipeCollections.mutate(next);
  }

  function handleCreate(event: FormEvent) {
    event.preventDefault();
    const trimmed = newName.trim();
    if (!trimmed || createCollection.isPending) return;
    createCollection.mutate(trimmed);
  }

  function handleRenameSubmit(event: FormEvent, collectionId: string) {
    event.preventDefault();
    const trimmed = renameValue.trim();
    if (!trimmed || renameCollection.isPending) return;
    renameCollection.mutate({ id: collectionId, name: trimmed });
  }

  const items = collections.data ?? [];

  return (
    <div className="coll">
      <button
        type="button"
        ref={buttonRef}
        className="coll__trigger"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        Collections{collectionIds.length > 0 ? ` (${collectionIds.length})` : ""}
      </button>

      {open ? (
        <div
          id={panelId}
          ref={panelRef}
          className="coll__panel"
          role="group"
          aria-label="Manage this recipe's collections"
        >
          <div className="coll__panel-head">
            <h3 className="coll__panel-title">Collections</h3>
            {items.length > 0 ? (
              <button
                type="button"
                className="coll__manage-toggle"
                onClick={() => {
                  setManageMode((value) => !value);
                  setRenamingId(null);
                  setDeletingId(null);
                }}
              >
                {manageMode ? "Done" : "Manage"}
              </button>
            ) : null}
          </div>

          {collections.isPending ? (
            <p className="coll__status">Loading…</p>
          ) : collections.isError ? (
            <p className="coll__status coll__status--error">
              {apiErrorMessage(collections.error, "Could not load collections.")}
            </p>
          ) : items.length === 0 ? (
            <p className="coll__status">No collections yet — add one below.</p>
          ) : (
            <ul className="coll__list">
              {items.map((collection) => (
                <li key={collection.id} className="coll__item">
                  {manageMode ? (
                    renamingId === collection.id ? (
                      <form
                        className="coll__rename-form"
                        onSubmit={(event) => handleRenameSubmit(event, collection.id)}
                      >
                        <input
                          className="input coll__rename-input"
                          aria-label={`Rename ${collection.name}`}
                          value={renameValue}
                          onChange={(event) => setRenameValue(event.target.value)}
                          autoFocus
                        />
                        <button
                          type="submit"
                          className="coll__text-btn"
                          disabled={renameCollection.isPending}
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          className="coll__text-btn"
                          onClick={() => setRenamingId(null)}
                        >
                          Cancel
                        </button>
                      </form>
                    ) : deletingId === collection.id ? (
                      <span className="coll__confirm">
                        <span className="coll__confirm-q">Delete "{collection.name}"?</span>
                        <button
                          type="button"
                          className="coll__text-btn coll__text-btn--danger"
                          disabled={deleteCollection.isPending}
                          onClick={() => deleteCollection.mutate(collection.id)}
                        >
                          {deleteCollection.isPending ? "Deleting…" : "Yes"}
                        </button>
                        <button
                          type="button"
                          className="coll__text-btn"
                          disabled={deleteCollection.isPending}
                          onClick={() => setDeletingId(null)}
                        >
                          No
                        </button>
                      </span>
                    ) : (
                      <div className="coll__row">
                        <span className="coll__name">
                          {collection.name}
                          <span className="coll__count"> ({collection.recipe_count})</span>
                        </span>
                        <span className="coll__manage-actions">
                          <button
                            type="button"
                            className="coll__text-btn"
                            onClick={() => {
                              setRenamingId(collection.id);
                              setRenameValue(collection.name);
                            }}
                          >
                            Rename
                          </button>
                          <button
                            type="button"
                            className="coll__text-btn coll__text-btn--danger"
                            onClick={() => setDeletingId(collection.id)}
                          >
                            Delete
                          </button>
                        </span>
                      </div>
                    )
                  ) : (
                    <label className="coll__checkbox-row">
                      <input
                        type="checkbox"
                        checked={collectionIds.includes(collection.id)}
                        disabled={setRecipeCollections.isPending}
                        onChange={(event) => toggle(collection.id, event.target.checked)}
                      />
                      <span className="coll__name">{collection.name}</span>
                      <span className="coll__count">{collection.recipe_count}</span>
                    </label>
                  )}
                </li>
              ))}
            </ul>
          )}

          {!manageMode ? (
            <form className="coll__create-form" onSubmit={handleCreate}>
              <label className="coll__create-label" htmlFor={newCollectionInputId}>
                New collection
              </label>
              <div className="coll__create-row">
                <input
                  id={newCollectionInputId}
                  className="input coll__create-input"
                  placeholder="e.g. Weeknight dinners"
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                />
                <button
                  type="submit"
                  className="coll__text-btn coll__text-btn--primary"
                  disabled={!newName.trim() || createCollection.isPending}
                >
                  {createCollection.isPending ? "Adding…" : "Add"}
                </button>
              </div>
            </form>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
