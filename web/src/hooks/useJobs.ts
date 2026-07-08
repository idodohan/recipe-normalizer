import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { hasActiveJob, type Job } from "../components/inbox/jobStatus";

async function fetchJobs(): Promise<Job[]> {
  const { data, error } = await api.GET("/api/jobs");
  if (error) throw error;
  return data;
}

/**
 * Single owner of the `["jobs"]` cache key. Pass `poll: true` to live-poll
 * while anything is queued/running (spec §8); react-query dedupes the
 * underlying request across callers that share the key.
 */
export function useJobs({ poll = false }: { poll?: boolean } = {}) {
  return useQuery({
    queryKey: ["jobs"],
    queryFn: fetchJobs,
    refetchInterval: (query) =>
      poll && hasActiveJob(query.state.data) ? 2500 : false,
  });
}
