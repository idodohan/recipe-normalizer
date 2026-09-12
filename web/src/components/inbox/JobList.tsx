import { apiErrorMessage } from "../../api/errors";
import { useJobs } from "../../hooks/useJobs";
import { EmptyState } from "../EmptyState";
import { ErrorState } from "../ErrorState";
import { Skeleton } from "../Skeleton";
import { JobRow } from "./JobRow";

export function JobList() {
  // Live progress (spec §8): poll while anything is queued/running, else rest.
  const jobs = useJobs({ poll: true });

  if (jobs.isPending) {
    return <Skeleton variant="rows" />;
  }
  if (jobs.isError) {
    return (
      <ErrorState
        message={apiErrorMessage(jobs.error, "Could not load your inbox.")}
        onRetry={() => void jobs.refetch()}
      />
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
    <ol className="job-list rn-stagger">
      {jobs.data.map((job, index) => (
        <JobRow key={job.id} job={job} index={index} />
      ))}
    </ol>
  );
}
