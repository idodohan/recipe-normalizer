import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { apiErrorMessage, friendlyMessage } from "../../api/errors";
import { toast } from "../../hooks/useToast";
import {
  INPUT_LABEL,
  STATUS_META,
  jobConfidence,
  jobProvenance,
  jobSummary,
  type Job,
} from "./jobStatus";

export function JobRow({ job, index = 0 }: { job: Job; index?: number }) {
  const queryClient = useQueryClient();
  const status = STATUS_META[job.status];
  const provenance = jobProvenance(job);
  const confidence = jobConfidence(job);
  const screenshotRef = job.artifacts?.["screenshot_ref"];

  const retry = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST("/api/jobs/{job_id}/retry", {
        params: { path: { job_id: job.id } },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
    // A silent failure here reads as a successful retry — the row keeps its
    // "failed" tag and the button just becomes clickable again.
    onError: (error) => {
      toast({
        title: "Could not retry this job",
        description: apiErrorMessage(error, "Please try again."),
        variant: "error",
      });
    },
  });

  return (
    <li className="job-row" style={{ "--stagger-index": index } as CSSProperties}>
      <div className="job-row__main">
        <span className="job-row__kind">{INPUT_LABEL[job.input_type]}</span>
        <span className="job-row__summary" title={job.url ?? undefined}>
          {jobSummary(job)}
        </span>
        {provenance ? (
          <span className="job-row__provenance">{provenance}</span>
        ) : null}
        {confidence !== null ? (
          <span className="job-row__confidence">
            {Math.round(confidence * 100)}% confident
          </span>
        ) : null}
      </div>

      {(job.status === "failed" || job.status === "not_a_recipe") && job.reason ? (
        <p className="job-row__reason">
          {friendlyMessage(job.reason)}{" "}
          {screenshotRef ? (
            <a href={`/api/files/${screenshotRef}`} target="_blank" rel="noreferrer">
              See screenshot →
            </a>
          ) : null}
        </p>
      ) : null}

      <div className="job-row__side">
        <span className={`job-tag ${status.tone}`}>{status.label}</span>
        {job.status === "needs_review" ? (
          <Link className="btn btn--primary btn--sm" to={`/jobs/${job.id}/review`}>
            Review
          </Link>
        ) : null}
        {job.status === "done" && job.produced_recipe_ids[0] ? (
          <Link
            className="btn btn--ghost btn--sm"
            to={`/recipes/${job.produced_recipe_ids[0]}`}
          >
            View recipe
          </Link>
        ) : null}
        {job.status === "failed" ? (
          <button
            className="btn btn--ghost btn--sm"
            type="button"
            disabled={retry.isPending}
            onClick={() => retry.mutate()}
          >
            {retry.isPending ? "Retrying…" : "Retry"}
          </button>
        ) : null}
      </div>
    </li>
  );
}
