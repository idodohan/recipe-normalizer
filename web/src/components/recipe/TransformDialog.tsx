import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { apiErrorEnvelope, apiErrorMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";
import { Dialog } from "../Dialog";
import "./transform-dialog.css";

type TransformDialogProps = {
  open: boolean;
  onClose: () => void;
  recipeId: string;
};

const EXAMPLES = ["Make it vegan", "Make it gluten-free", "Adapt for an air fryer"];

/**
 * "Transform" — a qualitative rewrite of the recipe (substitution, dietary
 * adaptation, cooking method), not a quantity change. Anyone who can read
 * the recipe (owner or shared-cookbook member) can transform it — the same
 * access check the backend uses for chat/Q&A — so this dialog is offered
 * unconditionally on RecipeDetailPage, not gated to the owner.
 *
 * `POST /api/ai/recipes/{recipe_id}/transform` never returns the draft
 * itself: on success it hands back `{job_id, recipe_id}` for the SAME
 * review gate every other ingestion job lands in, so a successful submit
 * closes this dialog and navigates straight to `/jobs/{job_id}/review`.
 *
 * The one response this dialog treats specially is the deterministic
 * scaling boundary: `ai.service.transform_recipe` refuses a pure
 * quantity-only instruction ("halve it", "for 8 servings") with a 422
 * `use_scale_feature` BEFORE any LLM call. That's not an error — it's the
 * expected shape for the wrong kind of request — so it renders as a
 * friendly inline hint pointing at the recipe's own Scale control, never a
 * red error toast, and never a navigation.
 */
export function TransformDialog({ open, onClose, recipeId }: TransformDialogProps) {
  const navigate = useNavigate();
  const [instruction, setInstruction] = useState("");
  const [scaleHint, setScaleHint] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // Fresh dialog every time it opens — a leftover instruction or scale hint
  // from a previous visit shouldn't greet the next one.
  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setInstruction("");
      setScaleHint(false);
    }
  }, [open]);

  const transform = useMutation({
    mutationFn: async (value: string) => {
      const { data, error } = await api.POST("/api/ai/recipes/{recipe_id}/transform", {
        params: { path: { recipe_id: recipeId } },
        body: { instruction: value },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      onClose();
      toast({
        title: "Transforming your recipe…",
        description: "Review your draft when it’s ready.",
        variant: "success",
      });
      navigate(`/jobs/${data.job_id}/review`);
    },
    onError: (error) => {
      const { code } = apiErrorEnvelope(error);
      if (code === "use_scale_feature") {
        // Deliberately NOT a toast/error state — see the component doc.
        setScaleHint(true);
        return;
      }
      toast({
        title: "Could not transform this recipe",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = instruction.trim();
    if (!trimmed || transform.isPending) return;
    setScaleHint(false);
    transform.mutate(trimmed);
  }

  function fillExample(text: string) {
    setInstruction(text);
    setScaleHint(false);
    inputRef.current?.focus();
  }

  function goToScale() {
    onClose();
    // Deferred a frame so Dialog's own close (which returns focus to the
    // trigger button) settles first, then the real destination — the
    // recipe's own Scale control — takes over both scroll and focus.
    requestAnimationFrame(() => {
      const scale = document.getElementById("scale-control");
      scale?.scrollIntoView({ behavior: "smooth", block: "center" });
      scale?.querySelector<HTMLElement>(".scale__preset")?.focus();
    });
  }

  return (
    <Dialog open={open} onClose={onClose} title="Transform this recipe" className="transform-dlg">
      <form onSubmit={handleSubmit}>
        <p className="transform-dlg__hint">
          Describe a qualitative change — a substitution, a dietary adaptation, a different
          cooking method. The result lands as a new draft for you to review, just like an
          import.
        </p>

        <label className="transform-dlg__label" htmlFor="transform-instruction">
          Instruction
        </label>
        <textarea
          id="transform-instruction"
          ref={inputRef}
          className="input transform-dlg__input"
          rows={3}
          placeholder="e.g. Make it vegan"
          value={instruction}
          onChange={(event) => {
            setInstruction(event.target.value);
            if (scaleHint) setScaleHint(false);
          }}
          disabled={transform.isPending}
          autoFocus
        />

        <div className="transform-dlg__examples" role="group" aria-label="Example instructions">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="transform-dlg__example"
              onClick={() => fillExample(example)}
              disabled={transform.isPending}
            >
              {example}
            </button>
          ))}
        </div>

        {scaleHint ? (
          <p className="transform-dlg__scale-hint" role="status">
            To change quantities, use the Scale control on the recipe — Transform is for
            qualitative changes, not scaling.{" "}
            <button type="button" className="transform-dlg__scale-link" onClick={goToScale}>
              Go to Scale control →
            </button>
          </p>
        ) : null}

        <div className="transform-dlg__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn--primary"
            disabled={!instruction.trim() || transform.isPending}
          >
            {transform.isPending ? "Transforming…" : "Transform"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
