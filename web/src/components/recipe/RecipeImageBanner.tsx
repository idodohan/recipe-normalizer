import { useRef } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { apiErrorMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";

type RecipeImageBannerProps = {
  recipeId: string;
  imageUrl: string | null;
  /**
   * Whether to render the add/replace/remove controls. Setting the image is
   * owner-only on the backend, so someone reading (or editing) a recipe they
   * reach through a shared cookbook still sees the photo but none of the
   * affordances.
   */
  canManage: boolean;
};

/**
 * Full-bleed photo banner above the recipe title, plus the owner controls
 * for managing it. The image itself reserves its aspect ratio up front
 * (no layout shift once it loads) and is lazy — it's below the fold on
 * slow connections relative to the toolbar.
 *
 * Upload goes through a plain multipart fetch (openapi-fetch doesn't have
 * a clean multipart body type) — same approach as SubmitPanel's file slot.
 * Remove uses the typed DELETE since it has no body.
 */
export function RecipeImageBanner({
  recipeId,
  imageUrl,
  canManage,
}: RecipeImageBannerProps) {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["recipe", recipeId] });
    void queryClient.invalidateQueries({ queryKey: ["recipes"] });
  };

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch(`/api/recipes/${recipeId}/image`, {
        method: "PUT",
        body: form,
        credentials: "include",
      });
      const body = (await response.json().catch(() => null)) as unknown;
      if (!response.ok) throw body ?? { error: { message: "Upload failed." } };
      return body;
    },
    onSuccess: () => {
      invalidate();
      toast({ title: "Photo updated", variant: "success" });
    },
    onError: (error) => {
      toast({
        title: "Could not upload photo",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE("/api/recipes/{recipe_id}/image", {
        params: { path: { recipe_id: recipeId } },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      invalidate();
      toast({ title: "Photo removed", variant: "success" });
    },
    onError: (error) => {
      toast({
        title: "Could not remove photo",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  const busy = upload.isPending || remove.isPending;

  function pickFile() {
    fileInput.current?.click();
  }

  const fileField = (
    <input
      ref={fileInput}
      type="file"
      className="rd__image-file-input"
      accept="image/png,image/jpeg,image/webp,image/gif"
      onChange={(event) => {
        const file = event.target.files?.[0];
        if (file) upload.mutate(file);
        event.target.value = "";
      }}
    />
  );

  // Nothing to show a non-owner when there's no photo — the empty slot is
  // only there to hold the "Add photo" button.
  if (!imageUrl) {
    if (!canManage) return null;
    return (
      <div className="rd__image-empty">
        <button
          type="button"
          className="btn btn--secondary btn--sm"
          disabled={busy}
          onClick={pickFile}
        >
          {upload.isPending ? "Uploading…" : "Add photo"}
        </button>
        {fileField}
      </div>
    );
  }

  return (
    <div className="rd__image">
      <img className="rd__image-img" src={imageUrl} alt="" loading="lazy" />
      <div className="rd__image-scrim" aria-hidden="true" />
      {canManage ? (
        <>
          <div className="rd__image-controls">
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              disabled={busy}
              onClick={pickFile}
            >
              {upload.isPending ? "Uploading…" : "Replace photo"}
            </button>
            <button
              type="button"
              className="btn btn--secondary btn--sm rd__image-remove"
              disabled={busy}
              onClick={() => remove.mutate()}
            >
              {remove.isPending ? "Removing…" : "Remove"}
            </button>
          </div>
          {fileField}
        </>
      ) : null}
    </div>
  );
}
