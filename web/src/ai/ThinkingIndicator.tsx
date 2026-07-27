import { useEffect, useState } from "react";

/** A turn this slow is worth reassuring about — see the note below. */
const REASSURE_AFTER_MS = 8_000;

/**
 * Small animated ellipsis shown in place of an assistant bubble's content
 * while a reply is in flight. Pure CSS animation — killed by the site-wide
 * `prefers-reduced-motion` rule in `base.css` (dots just sit still instead
 * of bouncing), so no reduced-motion handling is needed here.
 *
 * The turn is a single blocking POST (the API doesn't stream), so a long
 * answer can leave the dots bouncing for the better part of a minute with no
 * sign of progress. After `REASSURE_AFTER_MS` a line is added telling the
 * user that's expected rather than stuck. The indicator mounts with the
 * pending row and unmounts when the reply lands, so the timer measures the
 * wait itself — no elapsed-time plumbing needed from the caller.
 */
export function ThinkingIndicator() {
  const [slow, setSlow] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setSlow(true), REASSURE_AFTER_MS);
    return () => clearTimeout(timer);
  }, []);

  return (
    <>
      <span className="ai-thinking" role="status" aria-label="Thinking">
        <span className="ai-thinking__dot" />
        <span className="ai-thinking__dot" />
        <span className="ai-thinking__dot" />
      </span>
      {slow ? (
        <p className="ai-thinking__note">
          Still thinking — long recipes can take up to a minute.
        </p>
      ) : null}
    </>
  );
}
