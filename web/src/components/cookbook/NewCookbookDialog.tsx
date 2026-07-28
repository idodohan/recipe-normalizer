import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Dialog } from "../Dialog";
import { Button } from "../Button";
import { Field, Input } from "../Field";
import { useCreateCookbook } from "../../hooks/useCookbooks";
import { apiErrorMessage } from "../../api/errors";

export function NewCookbookDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const create = useCreateCookbook();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit() {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Give your cookbook a name.");
      return;
    }
    setError(null);
    create.mutate(
      { name: trimmed },
      {
        onSuccess: (cb) => {
          setName("");
          onClose();
          if (cb) navigate(`/cookbooks/${cb.id}`);
        },
        onError: (e) =>
          setError(apiErrorMessage(e, "Could not create the cookbook.")),
      },
    );
  }

  return (
    <Dialog open={open} onClose={onClose} title="New cookbook">
      <div className="dialog__body">
        <Field label="Name" error={error ?? undefined}>
          {(fieldProps) => (
            <Input
              {...fieldProps}
              value={name}
              autoFocus
              invalid={Boolean(error)}
              placeholder="e.g. Weeknight dinners"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submit();
                }
              }}
            />
          )}
        </Field>
      </div>
      <div className="dialog__footer">
        <Button variant="secondary" onClick={onClose} disabled={create.isPending}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create cookbook"}
        </Button>
      </div>
    </Dialog>
  );
}
