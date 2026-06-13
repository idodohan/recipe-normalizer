import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { components } from "../../api/schema";

type InputType = components["schemas"]["InputType"];

// Artifact values arrive from the API as ready /api/files/{ref} URL paths.
type Tab = { key: string; label: string; kind: "text" | "image" | "pdf"; url: string };

/** Build the ordered list of source tabs from a job's retained artifacts. */
function buildTabs(
  artifacts: Record<string, string>,
  inputType: InputType,
): Tab[] {
  const tabs: Tab[] = [];
  const text = artifacts["raw_text_ref"] ?? artifacts["readable_text_ref"];
  if (text) tabs.push({ key: "text", label: "Source text", kind: "text", url: text });

  if (inputType === "image" && artifacts["source_file_ref"]) {
    tabs.push({ key: "image", label: "Original image", kind: "image", url: artifacts["source_file_ref"] });
  }
  if (inputType === "pdf" && artifacts["source_file_ref"]) {
    tabs.push({ key: "pdf", label: "Original PDF", kind: "pdf", url: artifacts["source_file_ref"] });
  }

  // PDF vision pages are stored one key per page (page_image_ref_0, _1, …).
  Object.keys(artifacts)
    .filter((key) => key.startsWith("page_image_ref"))
    .sort()
    .forEach((key, index) => {
      tabs.push({ key, label: `Page ${index + 1}`, kind: "image", url: artifacts[key] });
    });

  const shot = artifacts["screenshot_ref"] ?? artifacts["tier3_screenshot_ref"];
  if (shot) tabs.push({ key: "shot", label: "Screenshot", kind: "image", url: shot });

  return tabs;
}

function TextArtifact({ url }: { url: string }) {
  const query = useQuery({
    queryKey: ["file-text", url],
    queryFn: async () => {
      const response = await fetch(url, { credentials: "include" });
      if (!response.ok) throw new Error("Could not load the source text.");
      return response.text();
    },
  });
  if (query.isPending) return <p className="source-panel__status">Loading source…</p>;
  if (query.isError) return <p className="source-panel__status">Source text unavailable.</p>;
  return <pre className="source-panel__text">{query.data}</pre>;
}

export function SourcePanel({
  artifacts,
  inputType,
  textPreview,
}: {
  artifacts: Record<string, string>;
  inputType: InputType;
  textPreview: string | null;
}) {
  const tabs = buildTabs(artifacts, inputType);
  const [active, setActive] = useState(0);

  if (tabs.length === 0) {
    return (
      <aside className="source-panel">
        <span className="source-panel__overline">Source</span>
        {textPreview ? (
          <pre className="source-panel__text">{textPreview}</pre>
        ) : (
          <p className="source-panel__status">
            No source artifact was retained for this job.
          </p>
        )}
      </aside>
    );
  }

  const current = tabs[Math.min(active, tabs.length - 1)];

  return (
    <aside className="source-panel">
      <div className="source-panel__head">
        <span className="source-panel__overline">Source</span>
        {tabs.length > 1 ? (
          <div className="source-panel__tabs" role="tablist">
            {tabs.map((tab, index) => (
              <button
                key={tab.key}
                type="button"
                role="tab"
                aria-selected={index === active}
                className={
                  index === active
                    ? "source-panel__tab is-active"
                    : "source-panel__tab"
                }
                onClick={() => setActive(index)}
              >
                {tab.label}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      <div className="source-panel__body">
        {current.kind === "text" ? <TextArtifact url={current.url} /> : null}
        {current.kind === "image" ? (
          <img className="source-panel__image" src={current.url} alt={current.label} />
        ) : null}
        {current.kind === "pdf" ? (
          <object
            className="source-panel__pdf"
            data={current.url}
            type="application/pdf"
          >
            <a href={current.url} target="_blank" rel="noreferrer">
              Open the original PDF →
            </a>
          </object>
        ) : null}
      </div>
    </aside>
  );
}
