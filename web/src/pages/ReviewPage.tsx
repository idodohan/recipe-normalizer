import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { DraftEditor } from "../components/review/DraftEditor";
import { SourcePanel } from "../components/review/SourcePanel";
import { jobConfidence } from "../components/inbox/jobStatus";
import "../components/recipe/recipe-editor.css";
import "./review.css";

export function ReviewPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const job = useQuery({
    queryKey: ["job", id],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/jobs/{job_id}", {
        params: { path: { job_id: id } },
      });
      if (error) throw error;
      return data;
    },
  });

  const drafts = job.data?.drafts ?? [];
  const unresolved = drafts.filter((draft) => !draft.is_verified);

  // Selection is DERIVED (no setState-in-effect): the user's pick if still
  // unresolved, otherwise the first unresolved draft.
  const effectiveId =
    selectedId && unresolved.some((draft) => draft.id === selectedId)
      ? selectedId
      : (unresolved[0]?.id ?? null);

  // Once the job resolves (no drafts left to review), return to the inbox.
  useEffect(() => {
    if (job.data && (drafts.length === 0 || unresolved.length === 0)) {
      navigate("/inbox", { replace: true });
    }
  }, [job.data, drafts.length, unresolved.length, navigate]);

  const recipe = useQuery({
    queryKey: ["recipe", effectiveId],
    enabled: Boolean(effectiveId),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/recipes/{recipe_id}", {
        params: { path: { recipe_id: effectiveId! } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (job.isPending) {
    return <p className="review-status">Loading the job…</p>;
  }
  if (job.isError) {
    return (
      <p className="review-status review-status--error" role="alert">
        {apiErrorMessage(job.error, "Could not load this job.")}
      </p>
    );
  }

  const detail = job.data;
  const total = unresolved.length;
  const position =
    unresolved.findIndex((draft) => draft.id === effectiveId) + 1 || 1;

  return (
    <div className="review">
      <header className="review__header">
        <div>
          <span className="review__overline">Review extraction</span>
          <h1 className="review__title">
            {detail.url ?? detail.filename ?? "Pasted text"}
          </h1>
        </div>
        <Link className="btn btn--ghost" to="/inbox">
          Back to inbox
        </Link>
      </header>

      {total > 1 ? (
        <div className="review__draft-tabs" role="tablist">
          {unresolved.map((draft, index) => (
            <button
              key={draft.id}
              type="button"
              role="tab"
              aria-selected={draft.id === effectiveId}
              className={
                draft.id === effectiveId
                  ? "review__draft-tab is-active"
                  : "review__draft-tab"
              }
              onClick={() => setSelectedId(draft.id)}
            >
              Recipe {index + 1} of {total}
              <span className="review__draft-tab-title">{draft.title}</span>
            </button>
          ))}
        </div>
      ) : null}

      <div className="review__split">
        <SourcePanel
          artifacts={detail.artifacts ?? {}}
          inputType={detail.input_type}
          textPreview={detail.text_preview ?? null}
        />
        <main className="review__draft">
          {total > 1 ? (
            <p className="review__draft-position">
              Recipe {position} of {total}
            </p>
          ) : null}
          {recipe.isPending || !effectiveId ? (
            <p className="review-status">Loading the draft…</p>
          ) : recipe.isError ? (
            <p className="review-status review-status--error" role="alert">
              {apiErrorMessage(recipe.error, "Could not load this draft.")}
            </p>
          ) : (
            <DraftEditor
              key={recipe.data.id}
              jobId={id}
              recipe={recipe.data}
              confidence={jobConfidence(detail)}
              onResolved={() => setSelectedId(null)}
            />
          )}
        </main>
      </div>
    </div>
  );
}
