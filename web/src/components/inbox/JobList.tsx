import { apiErrorMessage } from "../../api/errors";
import { useJobs } from "../../hooks/useJobs";
import { EmptyState } from "../EmptyState";
import { JobRow } from "./JobRow";

export function JobList() {
  // Live progress (spec §8): poll while anything is queued/running, else rest.
  const jobs = useJobs({ poll: true });

  if (jobs.isPending) {
    return <p className="inbox-status">Loading your inbox…</p>;
  }
  if (jobs.isError) {
    return (
      <p className="inbox-status inbox-status--error" role="alert">
        {apiErrorMessage(jobs.error, "Could not load your inbox.")}
      </p>
    );
  }
  if (jobs.data.length === 0) {
    return (
      <EmptyState
        title="Nothing in the inbox yet"
        body="Paste a link, drop a file, or add some text above. Extraction runs in the background and lands here for review."
      />
    );
  }

  return (
    <ol className="job-list">
      {jobs.data.map((job) => (
        <JobRow key={job.id} job={job} />
      ))}
    </ol>
  );
}
