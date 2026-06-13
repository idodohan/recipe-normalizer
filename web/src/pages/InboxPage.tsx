import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { Button } from "../components/Button";
import { PageHeader } from "../components/PageHeader";
import { JobList } from "../components/inbox/JobList";
import { SubmitPanel } from "../components/inbox/SubmitPanel";
import { type Job } from "../components/inbox/jobStatus";
import "./inbox.css";

export function InboxPage() {
  const queryClient = useQueryClient();

  // Shares the ["jobs"] cache with JobList (react-query dedupes the request).
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: async (): Promise<Job[]> => {
      const { data, error } = await api.GET("/api/jobs");
      if (error) throw error;
      return data;
    },
  });

  const reviewCount =
    jobs.data?.filter((job) => job.status === "needs_review").length ?? 0;

  const acceptAll = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/jobs/accept-high-confidence");
      if (error) throw error;
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  return (
    <>
      <PageHeader
        overline="Bring recipes in"
        title="Inbox"
        subtitle="Submit a source and watch it move through extraction. Drafts wait here for your review."
        action={
          reviewCount > 0 ? (
            <Button
              variant="secondary"
              disabled={acceptAll.isPending}
              onClick={() => acceptAll.mutate()}
            >
              {acceptAll.isPending
                ? "Approving…"
                : "Approve all high-confidence"}
            </Button>
          ) : undefined
        }
      />
      <SubmitPanel />
      <JobList />
    </>
  );
}
