import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { toast } from "../../hooks/useToast";
import type { components } from "../../api/schema";

type RecipeOut = components["schemas"]["RecipeOut"];

type NotesSectionProps = {
  recipeId: string;
  notes: string | null;
};

/**
 * Kitchen notes — marginalia, not a form field. Saves silently on blur;
 * only failures get a toast. The textarea grows with its content (CSS
 * `field-sizing` where supported, a scrollHeight fallback elsewhere).
 */
export function NotesSection({ recipeId, notes }: NotesSectionProps) {
  const queryClient = useQueryClient();
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [value, setValue] = useState(notes ?? "");
  const savedValueRef = useRef(notes ?? "");

  // If the server value changes under us (refetch, another tab) adopt it —
  // but never while the field is focused, so we don't clobber a live edit.
  useEffect(() => {
    if (document.activeElement !== textareaRef.current) {
      setValue(notes ?? "");
      savedValueRef.current = notes ?? "";
    }
  }, [notes]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [value]);

  const save = useMutation({
    mutationFn: async (nextNotes: string) => {
      const trimmed = nextNotes.length > 0 ? nextNotes : null;
      const { data, error } = await api.PATCH("/api/recipes/{recipe_id}/personal", {
        params: { path: { recipe_id: recipeId } },
        body: { notes: trimmed },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      savedValueRef.current = data.notes ?? "";
      queryClient.setQueryData<RecipeOut>(["recipe", recipeId], (prev) =>
        prev ? { ...prev, notes: data.notes } : prev,
      );
    },
    onError: () => {
      toast({
        title: "Could not save note",
        description: "Your change was not saved. Please try again.",
        variant: "error",
      });
    },
  });

  function handleBlur() {
    if (value === savedValueRef.current) return;
    save.mutate(value);
  }

  return (
    <section className="rd__notes">
      <h2 className="rd__notes-heading">Kitchen notes</h2>
      <textarea
        ref={textareaRef}
        className="rd__notes-input"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onBlur={handleBlur}
        placeholder="Add a note — substitutions, tweaks, who loved it…"
        rows={1}
      />
    </section>
  );
}
