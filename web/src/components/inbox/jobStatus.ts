import type { components } from "../../api/schema";

export type Job = components["schemas"]["JobOut"];
export type JobDetail = components["schemas"]["JobDetailOut"];
export type JobStatus = components["schemas"]["JobStatus"];
export type InputType = components["schemas"]["InputType"];

/** Small-caps editorial label + tone class per status. */
export const STATUS_META: Record<JobStatus, { label: string; tone: string }> = {
  queued: { label: "Queued", tone: "is-queued" },
  running: { label: "Running", tone: "is-running" },
  needs_review: { label: "Needs review", tone: "is-review" },
  done: { label: "Done", tone: "is-done" },
  failed: { label: "Failed", tone: "is-failed" },
  not_a_recipe: { label: "Not a recipe", tone: "is-failed" },
};

export const INPUT_LABEL: Record<InputType, string> = {
  url: "Link",
  pdf: "PDF",
  image: "Image",
  text: "Text",
};

/** True while at least one job is still being worked — drives live polling. */
export function hasActiveJob(jobs: Job[] | undefined): boolean {
  return (jobs ?? []).some(
    (job) => job.status === "queued" || job.status === "running",
  );
}

/** Short human summary of a job's input for the row title. */
export function jobSummary(job: Job): string {
  if (job.input_type === "url" && job.url) {
    try {
      return new URL(job.url).hostname.replace(/^www\./, "");
    } catch {
      return job.url;
    }
  }
  if (job.filename) return job.filename;
  if (job.text_preview) return job.text_preview;
  return INPUT_LABEL[job.input_type];
}

/** Confidence (0..1) from extraction_meta, when present. */
export function jobConfidence(job: {
  extraction_meta?: Record<string, unknown> | null;
}): number | null {
  const value = job.extraction_meta?.["confidence"];
  return typeof value === "number" ? value : null;
}

/** Tier / source provenance line ("Tier 2", "Scanned PDF"), when known. */
export function jobProvenance(job: Job): string | null {
  const meta = job.extraction_meta ?? {};
  const tier = meta["tier_used"];
  if (typeof tier === "number") return `Tier ${tier}`;
  const source = meta["source"];
  if (source === "pdf_text") return "PDF text layer";
  if (source === "pdf_vision") return "Scanned PDF";
  if (source === "image") return "Image";
  return null;
}
