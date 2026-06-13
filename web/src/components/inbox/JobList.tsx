import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { apiErrorMessage } from "../../api/errors";
import { EmptyState } from "../EmptyState";
import { JobRow } from "./JobRow";
import { hasActiveJob, type Job } from "./jobStatus";

async function fetchJobs(): Promise<Job[]> {
  const { data, error } = await api.GET("/api/jobs");
  if (error) throw error;
  return data;
}

export function JobList() {
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: fetchJobs,
    // Live progress (spec §8): poll while anything is queued/running, else rest.
    refetchInterval: (query) =>
      hasActiveJob(query.state.data as Job[] | undefined) ? 2500 : false,
  });

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
