import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../../api/client";

type Notice =
  | { kind: "error"; message: string }
  | { kind: "duplicate-recipe"; message: string; recipeId: string }
  | { kind: "duplicate-job"; message: string; jobId: string };

/** Pull the error envelope (code + message + extras) out of an openapi-fetch error. */
function readEnvelope(error: unknown): Notice {
  const err =
    error && typeof error === "object" && "error" in error
      ? (error as { error: Record<string, unknown> }).error
      : undefined;
  const message =
    typeof err?.["message"] === "string"
      ? (err["message"] as string)
      : "Could not submit. Please try again.";
  if (err?.["code"] === "duplicate_recipe" && typeof err["existing_id"] === "string") {
    return { kind: "duplicate-recipe", message, recipeId: err["existing_id"] };
  }
  if (err?.["code"] === "duplicate_job" && typeof err["job_id"] === "string") {
    return { kind: "duplicate-job", message, jobId: err["job_id"] };
  }
  return { kind: "error", message };
}

export function SubmitPanel() {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const onSettled = () => {
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
  };

  const submitUrl = useMutation({
    mutationFn: async (value: string) => {
      const { data, error } = await api.POST("/api/ingest/url", {
        body: { url: value },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setUrl("");
      setNotice(null);
      onSettled();
    },
    onError: (error) => setNotice(readEnvelope(error)),
  });

  const submitText = useMutation({
    mutationFn: async (value: string) => {
      const { data, error } = await api.POST("/api/ingest/text", {
        body: { text: value },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setText("");
      setNotice(null);
      onSettled();
    },
    onError: (error) => setNotice(readEnvelope(error)),
  });

  const submitFile = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/ingest/file", {
        method: "POST",
        body: form,
        credentials: "include",
      });
      const body = (await response.json().catch(() => null)) as unknown;
      if (!response.ok) throw body ?? { error: { message: "Upload failed." } };
      return body;
    },
    onSuccess: () => {
      setNotice(null);
      onSettled();
    },
    onError: (error) => setNotice(readEnvelope(error)),
  });

  const busy =
    submitUrl.isPending || submitText.isPending || submitFile.isPending;

  return (
    <section className="submit-panel" aria-label="Add a recipe source">
      <div className="submit-grid">
        {/* Link slot */}
        <form
          className="submit-slot"
          onSubmit={(event) => {
            event.preventDefault();
            if (url.trim()) submitUrl.mutate(url.trim());
          }}
        >
          <span className="submit-slot__label">Paste a link</span>
          <p className="submit-slot__hint">
            A recipe page anywhere on the web.
          </p>
          <div className="submit-slot__row">
            <input
              className="submit-slot__input"
              type="url"
              inputMode="url"
              placeholder="https://…"
              value={url}
              disabled={busy}
              onChange={(event) => setUrl(event.target.value)}
            />
            <button
              className="btn btn--primary"
              type="submit"
              disabled={busy || !url.trim()}
            >
              Fetch
            </button>
          </div>
        </form>

        {/* File slot */}
        <div
          className={
            dragging ? "submit-slot submit-slot--drop is-dragging" : "submit-slot submit-slot--drop"
          }
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            const file = event.dataTransfer.files?.[0];
            if (file) submitFile.mutate(file);
          }}
        >
          <span className="submit-slot__label">Drop a file</span>
          <p className="submit-slot__hint">PDF or photo of a recipe.</p>
          <button
            className="submit-slot__drop-target"
            type="button"
            disabled={busy}
            onClick={() => fileInput.current?.click()}
          >
            {submitFile.isPending ? "Uploading…" : "Drag here, or browse"}
          </button>
          <input
            ref={fileInput}
            className="submit-slot__file"
            type="file"
            accept="application/pdf,image/png,image/jpeg,image/webp,image/gif"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) submitFile.mutate(file);
              event.target.value = "";
            }}
          />
        </div>

        {/* Text slot */}
        <form
          className="submit-slot"
          onSubmit={(event) => {
            event.preventDefault();
            if (text.trim()) submitText.mutate(text.trim());
          }}
        >
          <span className="submit-slot__label">Paste text</span>
          <p className="submit-slot__hint">
            Typed or copied from somewhere else.
          </p>
          <textarea
            className="submit-slot__textarea"
            rows={3}
            placeholder="2 cups flour…"
            value={text}
            disabled={busy}
            onChange={(event) => setText(event.target.value)}
          />
          <button
            className="btn btn--primary submit-slot__text-submit"
            type="submit"
            disabled={busy || !text.trim()}
          >
            Add text
          </button>
        </form>
      </div>

      {notice ? (
        <p
          className={
            notice.kind === "error"
              ? "submit-notice submit-notice--error"
              : "submit-notice"
          }
          role="status"
        >
          {notice.message}{" "}
          {notice.kind === "duplicate-recipe" ? (
            <Link to={`/recipes/${notice.recipeId}`}>View the recipe →</Link>
          ) : null}
          {notice.kind === "duplicate-job" ? (
            <Link to={`/jobs/${notice.jobId}/review`}>Open the job →</Link>
          ) : null}
        </p>
      ) : null}
    </section>
  );
}
