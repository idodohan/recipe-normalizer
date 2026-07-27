import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { PageHeader } from "../components/PageHeader";
import { JobList } from "../components/inbox/JobList";
import { SubmitPanel } from "../components/inbox/SubmitPanel";
import { useJobs } from "../hooks/useJobs";
import { toast } from "../hooks/useToast";
import "./inbox.css";

export function InboxPage() {
  const queryClient = useQueryClient();

  // Shares the ["jobs"] cache with JobList (react-query dedupes the request).
  const jobs = useJobs();

  const reviewCount =
    jobs.data?.filter((job) => job.status === "needs_review").length ?? 0;

  const acceptAll = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/jobs/accept-high-confidence");
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      const count = data.accepted;
      toast({
        title: `${count} recipe${count === 1 ? "" : "s"} accepted`,
        variant: "success",
      });
    },
    // Without this a failure is indistinguishable from "0 accepted": the
    // button just stops spinning and the drafts stay put.
    onError: (error) => {
      toast({
        title: "Could not approve the drafts",
        description: apiErrorMessage(error, "Nothing was accepted. Please try again."),
        variant: "error",
      });
    },
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
